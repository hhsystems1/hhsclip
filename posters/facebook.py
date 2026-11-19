#!/usr/bin/env python3
"""
Facebook poster — Meta Graph API, Page Reels (video_reels edge).

Flow (Meta's Reels Publishing docs, Graph API v26.0):
  1. POST https://graph.facebook.com/{v}/{PAGE_ID}/video_reels
     upload_phase=start, access_token=<page token>
     -> {"video_id": ..., "upload_url": ...}
  2. POST the bytes to the upload_url on rupload.facebook.com with
     headers Authorization: OAuth <page token>, offset: 0,
     file_size: <bytes>
  3. POST https://graph.facebook.com/{v}/{PAGE_ID}/video_reels
     upload_phase=finish, video_id=<id>, video_state=PUBLISHED,
     description=<caption>
  4. Poll GET https://graph.facebook.com/{v}/{video_id}?fields=status
     until publishing_phase.publish_status == "published"

Reels publish to Pages only — there is no API for a personal profile
or a Group. Limit: 30 API-published Reels per Page per 24 hours.
Our clips are 30s, inside the 3s-90s Reels window.

Env: FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN. The Page token
may be the same long-lived Page token used for Instagram; if
FACEBOOK_PAGE_ACCESS_TOKEN is unset it falls back to
META_PAGE_ACCESS_TOKEN (documented in .env.example).
"""
import os
import time
from pathlib import Path

from .common import META_GRAPH_VERSION, PostError, http_bytes, http_json, load_dotenv, require_env

GRAPH = f"https://graph.facebook.com/{META_GRAPH_VERSION}"


def _token():
    load_dotenv()
    tok = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN") or os.environ.get("META_PAGE_ACCESS_TOKEN")
    if not tok:
        raise PostError("not configured: set FACEBOOK_PAGE_ACCESS_TOKEN (or META_PAGE_ACCESS_TOKEN) in .env")
    return tok


def configured():
    load_dotenv()
    return bool(os.environ.get("FACEBOOK_PAGE_ID") and
                (os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN") or
                 os.environ.get("META_PAGE_ACCESS_TOKEN")))


def post(video_path, title, description):
    """Publish one Reel to the Page. Returns (video_id, permalink).
    Raises PostError."""
    page_id = require_env("FACEBOOK_PAGE_ID")["FACEBOOK_PAGE_ID"]
    token = _token()
    caption = (title + "\n" + description)[:2200] if description else title[:2200]
    size = Path(video_path).stat().st_size
    # 1. Start.
    start = http_json("POST", f"{GRAPH}/{page_id}/video_reels", {
        "upload_phase": "start",
        "access_token": token,
    })
    video_id = start.get("video_id")
    upload_url = start.get("upload_url")
    if not video_id or not upload_url:
        raise PostError(f"Facebook Reels start failed: {str(start)[:300]}")
    data = Path(video_path).read_bytes()
    status, body = http_bytes("POST", upload_url, data, {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(size),
        "Content-Type": "video/mp4",
    }, timeout=300)
    if status not in (200, 201):
        raise PostError(f"Facebook upload failed (HTTP {status}): {body[:300]}")
    # 3. Finish + publish.
    fin = http_json("POST", f"{GRAPH}/{page_id}/video_reels", {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": caption,
        "access_token": token,
    })
    if fin.get("error"):
        raise PostError(f"Facebook finish failed: {str(fin)[:300]}")
    # 4. Poll until published.
    for _ in range(30):
        st = http_json("GET",
                       f"{GRAPH}/{video_id}?fields=status,permalink_url&access_token={token}")
        pub = (st.get("status") or {}).get("publishing_phase", {})
        if pub.get("publish_status") == "published":
            return video_id, st.get("permalink_url")
        if pub.get("publish_status") == "error":
            raise PostError(f"Facebook publishing error: {str(st)[:300]}")
        time.sleep(10)
    raise PostError("Facebook Reel still publishing after 5 min — check the Page, then retry if it never appeared")
