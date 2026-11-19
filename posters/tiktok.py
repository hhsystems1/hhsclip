#!/usr/bin/env python3
"""
TikTok poster — Content Posting API, Direct Post (scope `video.publish`).

Flow (per TikTok's docs, checked Nov 2026):
  1. GET /v2/post/publish/creator_info/query/ (mandatory before Direct Post)
  2. POST /v2/post/publish/video/init/ with post_info + source_info
     FILE_UPLOAD -> returns publish_id + upload_url
  3. PUT the file to upload_url in chunks of 5-64 MB (files < 5 MB go
     whole; last chunk may be up to 128 MB), in order
  4. Poll POST /v2/post/publish/status/fetch/ with publish_id until
     PUBLISH_COMPLETE / failed.

Privacy: SELF_ONLY by default. Until TikTok audits the app, every
Direct Post is restricted to private viewing anyway, and unaudited
clients may only post from private accounts (max 5 users / 24h).
Override with TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE only after the audit
passes — never before.

Env: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET (kept for the token
refresh flow), TIKTOK_ACCESS_TOKEN. TIKTOK_REFRESH_TOKEN optional.
Access tokens live 2h; refresh tokens 180 days (Nov 2026 docs).
"""
import json
import math
import os
from pathlib import Path

from .common import PostError, http_bytes, http_json, load_dotenv, require_env

BASE = "https://open.tiktokapis.com/v2"
CHUNK = 16 * 1024 * 1024  # 16 MB: inside TikTok's 5-64 MB chunk rule


def configured():
    load_dotenv()
    return bool(os.environ.get("TIKTOK_ACCESS_TOKEN"))


def privacy():
    p = os.environ.get("TIKTOK_PRIVACY", "SELF_ONLY").upper()
    if p not in ("SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS"):
        raise PostError(f"TIKTOK_PRIVACY must be SELF_ONLY/PUBLIC_TO_EVERYONE/MUTUAL_FOLLOW_FRIENDS, got {p!r}")
    return p


def _headers():
    token = require_env("TIKTOK_ACCESS_TOKEN")["TIKTOK_ACCESS_TOKEN"]
    return {"Authorization": f"Bearer {token}"}


def _refresh_if_needed():
    """Best-effort refresh if the access token is missing but a refresh
    token + client key/secret exist. Returns True if it set a token."""
    load_dotenv()
    if os.environ.get("TIKTOK_ACCESS_TOKEN"):
        return True
    rt = os.environ.get("TIKTOK_REFRESH_TOKEN")
    key, secret = os.environ.get("TIKTOK_CLIENT_KEY"), os.environ.get("TIKTOK_CLIENT_SECRET")
    if not (rt and key and secret):
        return False
    try:
        data = http_json("POST", "https://open.tiktokapis.com/v2/oauth/token/", {
            "client_key": key, "client_secret": secret,
            "grant_type": "refresh_token", "refresh_token": rt})
        if data.get("access_token"):
            os.environ["TIKTOK_ACCESS_TOKEN"] = data["access_token"]
            return True
    except PostError:
        pass
    return False


def post(video_path, title, description):
    """Direct Post one clip. Returns (publish_id, None) — TikTok gives
    no public URL at publish time; the receipt links the publish_id.
    Raises PostError."""
    _refresh_if_needed()
    headers = _headers()
    priv = privacy()
    size = Path(video_path).stat().st_size
    # 1. Creator info (mandatory before Direct Post).
    info = http_json("POST", BASE + "/post/publish/creator_info/query/",
                     {}, headers)
    if info.get("error", {}).get("code") not in (None, "ok"):
        raise PostError(f"TikTok creator_info failed: {str(info)[:300]}")
    # 2. Init.
    chunk_size = CHUNK if size > 5 * 1024 * 1024 else size
    total_chunks = max(1, math.ceil(size / chunk_size)) if size > 5 * 1024 * 1024 else 1
    init = http_json("POST", BASE + "/post/publish/video/init/", {
        "post_info": {
            "title": (title + "\n" + description)[:2200] if description else title[:2200],
            "privacy_level": priv,
            "disable_duet": False, "disable_comment": False, "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }, headers)
    data = init.get("data", {})
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not publish_id or not upload_url:
        raise PostError(f"TikTok init failed: {str(init)[:300]}")
    # 3. Chunked upload, in order. Each chunk -> 206, last chunk -> 201.
    with open(video_path, "rb") as fh:
        for i in range(total_chunks):
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            end = i * chunk_size + len(chunk) - 1
            status, body = http_bytes(
                "PUT", upload_url, chunk,
                {"Content-Range": f"bytes {i * chunk_size}-{end}/{size}",
                 "Content-Length": str(len(chunk)),
                 "Content-Type": "video/mp4"})
            if status not in (200, 201, 206):
                raise PostError(f"TikTok chunk {i + 1}/{total_chunks} failed (HTTP {status}): {body[:300]}")
    # 4. Poll status until terminal.
    import time
    for _ in range(30):
        st = http_json("POST", BASE + "/post/publish/status/fetch/",
                       {"publish_id": publish_id}, headers)
        s = st.get("data", {}).get("status")
        if s == "PUBLISH_COMPLETE":
            return publish_id, None
        if s in ("FAILED", "PUBLISH_FAILED"):
            raise PostError(f"TikTok publish failed: {str(st)[:300]}")
        time.sleep(10)
    # Upload accepted but still processing after ~5 min: return the id
    # so the receipt can be checked later; do not claim it posted.
    raise PostError(f"TikTok publish still processing after 5 min (publish_id {publish_id}) — retry status check later")
