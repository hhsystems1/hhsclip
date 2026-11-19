#!/usr/bin/env python3
"""
Instagram poster — Meta Graph API, Reels, Facebook Login path.

Flow (Meta's Content Publishing docs, Graph API v26.0):
  1. POST https://graph.facebook.com/{v}/{IG_USER_ID}/media
     media_type=REELS, upload_type=resumable, caption=...
     -> {"id": <container id>, "uri": <rupload.facebook.com URL>}
  2. POST the raw bytes to that uri with headers
     Authorization: OAuth <page token>, offset: 0, file_size: <bytes>
     (the uri already carries the container id)
  3. Poll GET https://graph.facebook.com/{v}/{container id}
     ?fields=status_code until FINISHED (max ~5 min)
  4. POST https://graph.facebook.com/{v}/{IG_USER_ID}/media_publish
     creation_id=<container id> -> {"id": <published media id>}

No public video URL is needed — the file goes up directly. That is
why the Facebook Login path is used first; the Instagram Login path
only accepts a public video_url.

Requires a Professional account (Business or Creator — personal
accounts cannot publish via the API) linked to a Facebook Page, and a
Page access token with instagram_basic + instagram_content_publish
(+ pages_read_engagement). Only for apps using Facebook Login for
Business — see SETUP.md.

Env: INSTAGRAM_USER_ID, META_PAGE_ACCESS_TOKEN.
"""
import os
import time
from pathlib import Path

from .common import META_GRAPH_VERSION, PostError, http_bytes, http_json, load_dotenv, require_env

GRAPH = f"https://graph.facebook.com/{META_GRAPH_VERSION}"


def configured():
    load_dotenv()
    return bool(os.environ.get("INSTAGRAM_USER_ID") and
                os.environ.get("META_PAGE_ACCESS_TOKEN"))


def post(video_path, title, description):
    """Publish one Reel. Returns (media_id, permalink or None).
    Raises PostError."""
    creds = require_env("INSTAGRAM_USER_ID", "META_PAGE_ACCESS_TOKEN")
    ig_id, token = creds["INSTAGRAM_USER_ID"], creds["META_PAGE_ACCESS_TOKEN"]
    caption = (title + "\n" + description)[:2200] if description else title[:2200]
    size = Path(video_path).stat().st_size
    # 1. Create the resumable container.
    container = http_json("POST", f"{GRAPH}/{ig_id}/media", {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "access_token": token,
    })
    container_id = container.get("id")
    uri = container.get("uri")
    if not container_id or not uri:
        raise PostError(f"Instagram container creation failed: {str(container)[:300]}")
    # 2. Upload the bytes to rupload.
    data = Path(video_path).read_bytes()
    status, body = http_bytes("POST", uri, data, {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(size),
        "Content-Type": "video/mp4",
    }, timeout=300)
    if status not in (200, 201):
        raise PostError(f"Instagram upload failed (HTTP {status}): {body[:300]}")
    # 3. Poll until FINISHED.
    for _ in range(30):
        st = http_json("GET",
                       f"{GRAPH}/{container_id}?fields=status_code&access_token={token}")
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PostError(f"Instagram container {code}: {str(st)[:300]}")
        time.sleep(10)
    else:
        raise PostError("Instagram container never reached FINISHED (5 min) — retry later")
    # 4. Publish.
    pub = http_json("POST", f"{GRAPH}/{ig_id}/media_publish", {
        "creation_id": container_id,
        "access_token": token,
    })
    media_id = pub.get("id")
    if not media_id:
        raise PostError(f"Instagram publish failed: {str(pub)[:300]}")
    permalink = None
    try:
        info = http_json("GET",
                         f"{GRAPH}/{media_id}?fields=permalink&access_token={token}")
        permalink = info.get("permalink")
    except PostError:
        pass
    return media_id, permalink
