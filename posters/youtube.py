#!/usr/bin/env python3
"""
YouTube poster — Data API v3, videos.insert (resumable upload).

A vertical video <= 3 minutes is filed by YouTube as a Short
automatically; there is no Shorts flag. Category 20 = Gaming.

Privacy: PRIVATE by default. Never defaults to public — projects
created after 28 Jul 2020 have their API uploads restricted to private
until Google passes the project's compliance audit, and even after
that, posting a GTA clip publicly should be a deliberate choice, not
a default. Override with YOUTUBE_PRIVACY=unlisted (or public, only
after the audit passes).

Env: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN.
Get a refresh token once with `python3 posters/youtube_oauth.py`
(see SETUP.md). No token is ever written to disk by this module.
"""
import json
import os
import urllib.request
from pathlib import Path

from .common import NotConfigured, PostError, http_bytes, http_json, load_dotenv, require_env

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024


def configured():
    load_dotenv()
    return all(os.environ.get(n) for n in
               ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"))


def privacy():
    p = os.environ.get("YOUTUBE_PRIVACY", "private").lower()
    if p not in ("private", "unlisted", "public"):
        raise PostError(f"YOUTUBE_PRIVACY must be private/unlisted/public, got {p!r}")
    return p


def _access_token():
    creds = require_env("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                        "YOUTUBE_REFRESH_TOKEN")
    body = {
        "client_id": creds["YOUTUBE_CLIENT_ID"],
        "client_secret": creds["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": creds["YOUTUBE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }
    data = http_json("POST", TOKEN_URL, body)
    token = data.get("access_token")
    if not token:
        raise PostError(f"could not refresh YouTube access token: {str(data)[:200]}")
    return token


def post(video_path, title, description):
    """Upload one clip. Returns (video_id, watch_url). Raises PostError."""
    token = _access_token()
    priv = privacy()
    size = Path(video_path).stat().st_size
    meta = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": priv},
    }
    # 1. Start a resumable session.
    req = urllib.request.Request(
        UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
        data=json.dumps(meta).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Upload-Content-Length", str(size))
    req.add_header("X-Upload-Content-Type", "video/mp4")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            session = resp.headers.get("Location")
    except Exception as e:
        raise PostError(f"YouTube resumable session failed: {e}")
    if not session:
        raise PostError("YouTube did not return an upload session URL")
    # 2. Upload the bytes in chunks.
    with open(video_path, "rb") as fh:
        offset = 0
        while offset < size:
            chunk = fh.read(CHUNK)
            end = offset + len(chunk) - 1
            status, body = http_bytes(
                "PUT", session, chunk,
                {"Content-Range": f"bytes {offset}-{end}/{size}",
                 "Content-Length": str(len(chunk))})
            if status in (200, 201):
                try:
                    video_id = json.loads(body)["id"]
                except Exception:
                    raise PostError(f"YouTube upload finished but response was odd: {body[:200]}")
                return video_id, f"https://www.youtube.com/watch?v={video_id}"
            if status != 308:  # 308 = resume incomplete, keep going
                raise PostError(f"YouTube chunk upload failed (HTTP {status}): {body[:300]}")
            offset = end + 1
    raise PostError("YouTube upload ended without a video id")
