#!/usr/bin/env python3
"""
Shared helpers for the slice-2 posters. Stdlib only — no `requests`.

Rules baked in here, so no adapter can forget them:
- Secrets come from environment variables only (a `.env` file in the
  project root is loaded into os.environ if present, values already set
  in the real environment win). Never log a token.
- Dry-run is the default everywhere: dry_run=True means zero network
  calls to any platform, only a receipt describing what *would* be posted.
- Privacy defaults are the safest ones: YouTube PRIVATE, TikTok SELF_ONLY.
  A platform only goes public if the user explicitly sets the env var
  AND their developer app has passed that platform's audit.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")

# Graph API version used by the Meta adapters. v26.0 is the version
# Meta's own publishing examples use as of Oct-Nov 2026.
META_GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v26.0")

_ENV_LOADED = False


def load_dotenv(root=None):
    """Load `.env` (KEY=VALUE lines) into os.environ without overwriting
    values already present. No-op if the file is absent. Never prints values."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    root = Path(root or Path(__file__).resolve().parent.parent)
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PostError(Exception):
    """A real posting attempt failed. Carries a human-readable message
    (never a token). The clip stays retryable."""


class NotConfigured(PostError):
    """Credentials for this platform are absent. Dry-run still works."""


def require_env(*names):
    """Return a dict of env values, or raise NotConfigured naming the
    first missing variable (name only — never a value)."""
    load_dotenv()
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise NotConfigured(
            "not configured: set " + ", ".join(missing) + " in .env (see .env.example)"
        )
    return {n: os.environ[n] for n in names}


def http_json(method, url, body=None, headers=None, timeout=60):
    """One JSON request with urllib. Returns parsed JSON (or {} for an
    empty body). Raises PostError with the platform's error text."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise PostError(f"{method} {url.split('?')[0]} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise PostError(f"network error calling {url.split('?')[0]}: {e.reason}")


def http_bytes(method, url, data, headers=None, timeout=120):
    """Raw-bytes request (chunk uploads). Returns (status_code, body_text)."""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:500]


def receipt(platform, status, title, privacy, dry_run, external_id=None,
            url=None, error=None):
    """The receipt shape recorded in clips.json for every attempt."""
    return {
        "platform": platform,
        "status": status,  # dry_run | posted | failed | not_configured
        "title": title,
        "privacy": privacy,
        "dry_run": bool(dry_run),
        "external_id": external_id,
        "url": url,
        "error": error,
        "timestamp_utc": utc_now(),
    }
