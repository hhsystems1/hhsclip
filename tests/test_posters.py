#!/usr/bin/env python3
"""Slice-2 tests — mocked HTTP only. No test here ever touches a real
platform. Run:  python3 -m unittest discover -s tests -v

Covers the task's checks:
- pending/rejected clips are refused by the gate (no network call)
- dry-run makes zero network calls and still writes a receipt
- receipt shape (platform/status/privacy/timestamp)
- privacy defaults: YouTube PRIVATE, TikTok SELF_ONLY
- live post with no credentials -> "not_configured", clip stays approved
- a mocked successful adapter post -> "posted" receipt with id/url
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import posters.gate as gate
from posters import facebook, instagram, tiktok, youtube
from posters.common import load_dotenv

JOB = {
    "job": "job-test", "created_utc": "20261118-000000",
    "source": "input/test.mp4", "source_duration": 30.0,
    "owned": True, "attested_at": "2026-11-18T00:00:00Z",
    "mode_requested": "gameplay", "mode": "gameplay",
    "clips": [
        {"id": "clip-01", "job": "job-test", "rank": 1, "start": 0.0, "end": 30.0,
         "duration": 30.0, "score": 0.9, "video": "clip-01.mp4",
         "draft_title": "GTA 6 — test clip", "draft_description": "test desc #GTA6",
         "status": "approved"},
        {"id": "clip-02", "job": "job-test", "rank": 2, "start": 30.0, "end": 60.0,
         "duration": 30.0, "score": 0.5, "video": "clip-02.mp4",
         "draft_title": "pending one", "draft_description": "x", "status": "pending"},
        {"id": "clip-03", "job": "job-test", "rank": 3, "start": 60.0, "end": 90.0,
         "duration": 30.0, "score": 0.4, "video": "clip-03.mp4",
         "draft_title": "rejected one", "draft_description": "x", "status": "rejected"},
    ],
}


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig_out = gate.OUT
        gate.OUT = self.tmp
        job_dir = self.tmp / "job-test"
        job_dir.mkdir()
        (job_dir / "clips.json").write_text(json.dumps(copy.deepcopy(JOB)))
        for name in ("clip-01.mp4", "clip-02.mp4", "clip-03.mp4"):
            (job_dir / name).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
        # No credentials in the test env, and block .env loading so a
        # real .env on the developer's machine can't leak into tests.
        self.env_patch = mock.patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        import posters.common as _common
        _common._ENV_LOADED = True

    def tearDown(self):
        self.env_patch.stop()
        gate.OUT = self.orig_out
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest(self):
        return json.loads((self.tmp / "job-test" / "clips.json").read_text())

    def test_refuses_pending(self):
        with self.assertRaises(gate.GateError) as ctx:
            gate.post_clip("clip-02", ["youtube"], dry_run=False)
        self.assertIn("pending", str(ctx.exception))
        self.assertNotIn("posts", self._manifest()["clips"][1])

    def test_refuses_rejected(self):
        with self.assertRaises(gate.GateError) as ctx:
            gate.post_clip("clip-03", ["all"], dry_run=True)
        self.assertIn("rejected", str(ctx.exception))

    def test_unknown_clip_and_platform(self):
        with self.assertRaises(gate.GateError):
            gate.post_clip("clip-99", ["youtube"], dry_run=True)
        with self.assertRaises(gate.GateError):
            gate.post_clip("clip-01", ["myspace"], dry_run=True)

    def test_dry_run_makes_no_network_calls(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("network call in dry-run!")):
            receipts = gate.post_clip("clip-01", ["all"], dry_run=True)
        self.assertEqual(len(receipts), 4)
        for r in receipts:
            self.assertEqual(r["status"], "dry_run")
            self.assertTrue(r["dry_run"])
            self.assertIn("platform", r)
            self.assertIn("timestamp_utc", r)
            self.assertIsNone(r["error"])
        saved = self._manifest()["clips"][0]["posts"]
        self.assertEqual(len(saved), 4)
        self.assertTrue(all(p["status"] == "dry_run" for p in saved))

    def test_privacy_defaults(self):
        self.assertEqual(youtube.privacy(), "private")
        self.assertEqual(tiktok.privacy(), "SELF_ONLY")

    def test_live_without_credentials_is_not_configured(self):
        receipts = gate.post_clip("clip-01", ["all"], dry_run=False)
        self.assertTrue(all(r["status"] == "not_configured" for r in receipts))
        clip = self._manifest()["clips"][0]
        self.assertEqual(clip["status"], "approved")  # stays retryable
        self.assertTrue(all("not configured" in (p["error"] or "") for p in clip["posts"]))

    def test_mocked_success_records_posted_receipt(self):
        with mock.patch.object(youtube, "post", return_value=("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")), \
             mock.patch.object(youtube, "configured", return_value=True):
            receipts = gate.post_clip("clip-01", ["youtube"], dry_run=False)
        r = receipts[0]
        self.assertEqual(r["status"], "posted")
        self.assertEqual(r["external_id"], "dQw4w9WgXcQ")
        self.assertIn("youtube.com/watch", r["url"])
        self.assertEqual(r["privacy"], "private")
        self.assertFalse(r["dry_run"])

    def test_mocked_failure_stays_retryable(self):
        from posters.common import PostError
        with mock.patch.object(tiktok, "post", side_effect=PostError("TikTok init failed: HTTP 401")), \
             mock.patch.object(tiktok, "configured", return_value=True):
            receipts = gate.post_clip("clip-01", ["tiktok"], dry_run=False)
        self.assertEqual(receipts[0]["status"], "failed")
        self.assertIn("401", receipts[0]["error"])
        self.assertEqual(self._manifest()["clips"][0]["status"], "approved")

    def test_configured_map_false_without_creds(self):
        self.assertEqual(gate.configured_map(),
                         {"youtube": False, "tiktok": False,
                          "instagram": False, "facebook": False})


class YouTubeHttpTest(unittest.TestCase):
    """One mocked-HTTP pass through the real YouTube adapter: token
    refresh -> resumable session -> chunk PUT -> video id. Proves the
    adapter's wire shape without touching Google."""

    class Resp:
        def __init__(self, body, headers=None, status=200):
            self._body = body
            self.headers = headers or {}
            self.status = status

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_resumable_upload_flow(self):
        import urllib.request
        calls = []

        def fake_urlopen(req, timeout=60):
            url = req.full_url
            calls.append(url)
            if "oauth2.googleapis.com/token" in url:
                return self.Resp(b'{"access_token": "ya29.test"}')
            if "upload/youtube" in url and req.method == "POST":
                return self.Resp(b"{}", headers={"Location": "https://upload.example/session/1"})
            if "upload.example" in url:
                return self.Resp(b'{"id": "abc123"}', status=201)
            raise AssertionError(f"unexpected URL {url}")

        video = Path(tempfile.mktemp(suffix=".mp4"))
        video.write_bytes(b"\x00" * 2048)
        env = {"YOUTUBE_CLIENT_ID": "cid", "YOUTUBE_CLIENT_SECRET": "csec",
               "YOUTUBE_REFRESH_TOKEN": "rtok"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            vid, url = youtube.post(str(video), "GTA 6 — test", "desc")
        self.assertEqual(vid, "abc123")
        self.assertIn("watch?v=abc123", url)
        self.assertTrue(any("oauth2" in c for c in calls))
        self.assertTrue(any("upload/youtube" in c for c in calls))
        video.unlink()


if __name__ == "__main__":
    unittest.main()
