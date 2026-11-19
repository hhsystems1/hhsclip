#!/usr/bin/env python3
"""
Slice-2 posting CLI. Dry-run is the default — a real post needs --live.

  python3 post.py clip-02 --platform youtube --dry-run   # default: no network
  python3 post.py clip-02 --platform all --live          # real posts, approved clip only
  python3 post.py clip-02 --platform tiktok,instagram --live

Only clips with status "approved" in clips.json can post. Pending or
rejected clips are refused before any network call. Every attempt is
recorded as a receipt on the clip in clips.json.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from posters.common import load_dotenv  # noqa: E402
from posters.gate import GateError, configured_map, post_clip  # noqa: E402


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Post an approved clip (slice 2)")
    ap.add_argument("clip_id", help="e.g. clip-02")
    ap.add_argument("--platform", default="all",
                    help="youtube, tiktok, instagram, facebook, all, or comma list")
    ap.add_argument("--job", default=None, help="job dir name, e.g. job-20261118-174452")
    ap.add_argument("--dry-run", action="store_true",
                    help="log what would be posted; make no network calls (default)")
    ap.add_argument("--live", action="store_true",
                    help="actually post. Requires the platform to be configured in .env")
    args = ap.parse_args()
    dry_run = not args.live
    platforms = [p.strip() for p in args.platform.split(",") if p.strip()]
    print(f"Configured: {json.dumps(configured_map())}")
    print(f"Mode: {'DRY RUN — no network calls' if dry_run else 'LIVE — will post for real'}")
    try:
        receipts = post_clip(args.clip_id, platforms, dry_run=dry_run, job_name=args.job)
    except GateError as e:
        print(f"REFUSED: {e}")
        sys.exit(2)
    for r in receipts:
        line = f"[{r['platform']}] {r['status']} — privacy {r['privacy']}"
        if r.get("url"):
            line += f" — {r['url']}"
        elif r.get("external_id"):
            line += f" — id {r['external_id']}"
        if r.get("error"):
            line += f" — {r['error']}"
        if r.get("note"):
            line += f"\n    {r['note']}"
        print(line)
    failed = [r for r in receipts if r["status"] in ("failed", "not_configured")]
    if failed:
        print(f"{len(failed)} platform(s) did not post — clip stays approved and retryable.")
        sys.exit(1)


if __name__ == "__main__":
    main()
