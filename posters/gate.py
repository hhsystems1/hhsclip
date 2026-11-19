#!/usr/bin/env python3
"""
The approval gate. Every post — CLI or dashboard — goes through here.

Rules (non-negotiable, slice 2 + v1):
1. ONLY clips whose clips.json status is "approved" can post.
   Pending or rejected clips are refused with a clear error and no
   network call is made for them.
2. Dry-run is the default. A real post needs dry_run=False AND a
   caller that explicitly asked for live (the CLI's --live flag, or
   the dashboard's dry-run toggle switched off + Post tap).
   Dry-run makes ZERO network calls and still writes a receipt.
3. If a platform is not configured, a live attempt records a
   "not_configured" receipt and the clip stays approved / retryable.
   A failed post records "failed" with the error text and stays
   retryable. Nothing here ever flips a clip to "posted" — the
   per-platform receipts in clips.json are the record.
4. Own content only: the job's manifest must carry "owned": true
   (attested when the job was created). Jobs without an ownership
   attestation are refused, even if a clip in them is approved.
"""
import json
from pathlib import Path

from . import facebook, instagram, tiktok, youtube
from .common import META_GRAPH_VERSION, NotConfigured, PostError, load_dotenv, receipt, utc_now  # noqa: F401

ADAPTERS = {
    "youtube": youtube,
    "tiktok": tiktok,
    "instagram": instagram,
    "facebook": facebook,
}

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"


class GateError(Exception):
    """Refused by the gate (not approved, unknown clip/platform)."""


def _load_jobs():
    jobs = []
    if OUT.exists():
        for job_dir in sorted(OUT.iterdir(), reverse=True):
            mf = job_dir / "clips.json"
            if job_dir.is_dir() and mf.exists():
                try:
                    jobs.append(json.loads(mf.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
    return jobs


def find_clip(clip_id, job_name=None):
    for job in _load_jobs():
        if job_name and job["job"] != job_name:
            continue
        for clip in job["clips"]:
            if clip["id"] == clip_id:
                return job, clip
    return None, None


def save_job(job):
    (OUT / job["job"] / "clips.json").write_text(
        json.dumps(job, indent=2), encoding="utf-8")


def privacy_for(platform):
    """The privacy that would be used for this platform right now."""
    if platform == "youtube":
        return youtube.privacy()
    if platform == "tiktok":
        return tiktok.privacy()
    return "n/a"  # Instagram / Facebook publish to the account/Page as-is


def configured_map():
    load_dotenv()
    return {name: mod.configured() for name, mod in ADAPTERS.items()}


def post_clip(clip_id, platforms, dry_run=True, job_name=None):
    """Post one approved clip to `platforms` (list of names, or ["all"]).

    Returns a list of receipts (one per platform). Each receipt is also
    appended to the clip's "posts" list in clips.json and saved.
    Raises GateError if the clip is not approved.
    """
    load_dotenv()
    if platforms == ["all"] or "all" in platforms:
        platforms = list(ADAPTERS)
    unknown = [p for p in platforms if p not in ADAPTERS]
    if unknown:
        raise GateError(f"unknown platform(s): {', '.join(unknown)}")
    job, clip = find_clip(clip_id, job_name)
    if clip is None:
        raise GateError(f"clip not found: {clip_id}")
    if not job.get("owned"):
        raise GateError(
            f"job {job['job']} has no ownership attestation — this app is "
            "for your own videos only. Re-run the clipper with --i-own-it, "
            "or tick the ownership box in the app, to attest it.")
    if clip.get("status") != "approved":
        raise GateError(
            f"clip {clip_id} is {clip.get('status')!r} — only approved clips "
            "can post. Approve it in the queue first.")
    video = OUT / job["job"] / clip["video"]
    if not video.exists():
        raise GateError(f"clip file missing: {video}")
    title = clip.get("draft_title", f"Clip — {clip_id}")
    description = clip.get("draft_description", "")
    receipts = []
    clip.setdefault("posts", [])
    for platform in platforms:
        mod = ADAPTERS[platform]
        priv = privacy_for(platform)
        if dry_run:
            r = receipt(platform, "dry_run", title, priv, dry_run=True,
                        error=None)
            r["note"] = ("dry run — no network call made; would post "
                         f"{video.name} ({clip.get('duration')}s) as "
                         f"{priv if priv != 'n/a' else 'a Reel/video'}")
            receipts.append(r)
            clip["posts"].append(r)
            continue
        if not mod.configured():
            r = receipt(platform, "not_configured", title, priv, dry_run=False,
                        error=f"not configured: see .env.example for {platform}")
            receipts.append(r)
            clip["posts"].append(r)
            continue
        try:
            external_id, url = mod.post(str(video), title, description)
            r = receipt(platform, "posted", title, priv, dry_run=False,
                        external_id=external_id, url=url)
        except (PostError, NotConfigured) as e:
            r = receipt(platform, "failed", title, priv, dry_run=False,
                        error=str(e)[:500])
        except Exception as e:  # never let one platform kill the others
            r = receipt(platform, "failed", title, priv, dry_run=False,
                        error=f"{type(e).__name__}: {e}"[:500])
        receipts.append(r)
        clip["posts"].append(r)
    save_job(job)
    return receipts
