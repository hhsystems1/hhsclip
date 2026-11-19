#!/usr/bin/env python3
"""HHS Clips review-export tests — POST /api/jobs/<id>/export-review.

Run:  python3 -m unittest discover -s tests -v

- The endpoint writes review/<job-id>/ with index.html (a byte copy of
  review.html), review.json (clips + title), and the clip mp4s.
- review.json carries ingest {url, token} only when
  HHS_REVIEW_INGEST_URL / HHS_REVIEW_INGEST_TOKEN are set; without
  them there is no ingest key and the review page keeps decisions
  in the browser only.
- Missing job -> 404; job with no clips -> 400.
- The export never touches the job's clips.json.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server

REPO = Path(__file__).resolve().parent.parent


class ExportReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = (server.ROOT, server.OUT, server.INPUT)
        server.ROOT = self.tmp
        server.OUT = self.tmp / "output"
        server.INPUT = self.tmp / "input"
        server.OUT.mkdir(parents=True)
        server.INPUT.mkdir(parents=True)
        # The export copies the real review.html from the server dir.
        shutil.copyfile(REPO / "review.html", self.tmp / "review.html")
        # Env vars the export reads — save and clear, tests opt in.
        self.saved_env = {k: os.environ.pop(k, None) for k in
                          ("HHS_REVIEW_INGEST_URL", "HHS_REVIEW_INGEST_TOKEN")}
        job_dir = server.OUT / "job-demo"
        job_dir.mkdir()
        manifest = {
            "job": "job-demo", "created_utc": "20261119-000000",
            "source": "/somewhere/HansRosling_2014S.mp4", "owned": True,
            "mode_requested": "talking", "mode": "talking", "clips": [
                {"id": "clip-01", "job": "job-demo", "rank": 1,
                 "start": 510, "end": 540, "duration": 30, "score": 0.84,
                 "video": "clip-01.mp4", "status": "pending",
                 "draft_title": "What shall we do?",
                 "draft_description": "d", "reason": "strong opening line",
                 "excerpt": "What shall we do about this?"},
                {"id": "clip-04", "job": "job-demo", "rank": 2,
                 "start": 910, "end": 940, "duration": 30, "score": 0.82,
                 "video": "clip-04.mp4", "status": "approved",
                 "draft_title": "huge hump in the middle",
                 "draft_description": "d", "reason": "story turn",
                 "excerpt": "huge hump in the middle already have girls"},
            ]}
        (job_dir / "clips.json").write_text(json.dumps(manifest))
        (job_dir / "clip-01.mp4").write_bytes(b"\x00" * 128)
        (job_dir / "clip-04.mp4").write_bytes(b"\x01" * 256)
        self.manifest_before = (job_dir / "clips.json").read_bytes()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.ROOT, server.OUT, server.INPUT = self.orig
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path):
        import urllib.request
        req = urllib.request.Request(self._url(path), data=b"",
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_export_writes_folder(self):
        status, body = self._post("/api/jobs/job-demo/export-review")
        self.assertEqual(status, 200)
        dest = Path(body["dir"])
        self.assertTrue(dest.is_dir())
        self.assertEqual(body["job"], "job-demo")
        self.assertEqual(body["files"],
                         ["index.html", "review.json",
                          "clip-01.mp4", "clip-04.mp4"])
        self.assertEqual(body["missing"], [])
        self.assertFalse(body["ingest_configured"])
        # index.html is a byte copy of review.html
        self.assertEqual((dest / "index.html").read_bytes(),
                         (REPO / "review.html").read_bytes())
        # review.json shape
        review = json.loads((dest / "review.json").read_text())
        self.assertEqual(review["job"], "job-demo")
        self.assertIn("HansRosling_2014S.mp4", review["title"])
        self.assertNotIn("ingest", review)
        self.assertEqual(len(review["clips"]), 2)
        c1 = review["clips"][0]
        self.assertEqual(c1["id"], "clip-01")
        self.assertEqual(c1["title"], "What shall we do?")
        self.assertEqual(c1["video"], "clip-01.mp4")
        self.assertEqual(c1["start"], 510)
        self.assertEqual(c1["score"], 0.84)
        self.assertEqual(c1["status"], "pending")
        self.assertEqual(c1["reason"], "strong opening line")
        self.assertIn("netlify drop", body["next"].lower())
        # mp4s copied byte-identical
        self.assertEqual((dest / "clip-01.mp4").read_bytes(), b"\x00" * 128)
        self.assertEqual((dest / "clip-04.mp4").read_bytes(), b"\x01" * 256)
        # the job manifest is untouched
        self.assertEqual((server.OUT / "job-demo" / "clips.json").read_bytes(),
                         self.manifest_before)

    def test_export_includes_ingest_when_env_set(self):
        with mock.patch.dict(os.environ, {
                "HHS_REVIEW_INGEST_URL":
                    "https://core.helpinghandssystems.com/api/ingest/events",
                "HHS_REVIEW_INGEST_TOKEN": "tok-123"}):
            status, body = self._post("/api/jobs/job-demo/export-review")
        self.assertEqual(status, 200)
        self.assertTrue(body["ingest_configured"])
        review = json.loads(
            (Path(body["dir"]) / "review.json").read_text())
        self.assertEqual(review["ingest"], {
            "url": "https://core.helpinghandssystems.com/api/ingest/events",
            "token": "tok-123"})

    def test_export_404_for_missing_job(self):
        status, body = self._post("/api/jobs/job-nope/export-review")
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])

    def test_export_400_for_job_with_no_clips(self):
        empty = server.OUT / "job-empty"
        empty.mkdir()
        (empty / "clips.json").write_text(json.dumps({
            "job": "job-empty", "created_utc": "x", "source": "s",
            "owned": True, "clips": []}))
        status, body = self._post("/api/jobs/job-empty/export-review")
        self.assertEqual(status, 400)
        self.assertIn("no clips", body["error"])

    def test_build_review_json_has_no_ingest_without_env(self):
        job = {"job": "j", "source": "/a/b.mp4", "created_utc": "x",
               "mode": "talking", "clips": [
                   {"id": "clip-01", "draft_title": "t", "video": "c.mp4",
                    "start": 0, "end": 30, "duration": 30, "score": 0.5,
                    "reason": "r", "excerpt": "e", "status": "pending"}]}
        out = server.build_review_json(job)
        self.assertNotIn("ingest", out)
        self.assertEqual(out["clips"][0]["title"], "t")
        self.assertNotIn("draft_description", out["clips"][0])


if __name__ == "__main__":
    unittest.main()
