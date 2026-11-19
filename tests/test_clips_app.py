#!/usr/bin/env python3
"""HHS Clips v1 tests — modes, talking-head scoring, own-content gate.
Run:  python3 -m unittest discover -s tests -v

- resolve_mode: auto picks talking for speech-dense sources, gameplay
  for sparse ones, gameplay when there is no transcript.
- talking-head scoring: a window opening on a hook + question outranks
  a flat window; a window with no speech scores 0.
- clipper gate: no job starts without --i-own-it (refused before any
  download or cut); with the flag the job proceeds to its next step.
- server jobs gate: POST /api/jobs without the ownership attestation
  is refused (400); with it the job starts and the clipper is invoked
  with --i-own-it.
- posting gate: a job without an ownership attestation cannot post,
  even if a clip in it is approved.
"""
import copy
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

import clipper
import server


def _words_from(text, start=0.0, step=0.4):
    out, t = [], start
    for w in text.split():
        out.append({"start": round(t, 2), "end": round(t + 0.3, 2), "word": w})
        t += step
    return out


HOOK_TEXT = "Here's the secret nobody tells you about pricing"
Q_TEXT = "Why do most people get this wrong"
STORY_TEXT = "It turns out the answer is simple and it changed everything"
FLAT_TEXT = "The report shows the numbers for the quarter and the team reviewed the results in the meeting"


class ResolveModeTest(unittest.TestCase):
    def test_auto_talking_when_speech_dense(self):
        words = _words_from("word " * 300)
        self.assertEqual(clipper.resolve_mode("auto", words, 150.0), "talking")

    def test_auto_gameplay_when_speech_sparse(self):
        words = _words_from("word " * 120)
        self.assertEqual(clipper.resolve_mode("auto", words, 150.0), "gameplay")

    def test_auto_gameplay_with_no_transcript(self):
        self.assertEqual(clipper.resolve_mode("auto", [], 150.0), "gameplay")

    def test_explicit_modes_pass_through(self):
        words = _words_from("word " * 300)
        self.assertEqual(clipper.resolve_mode("talking", words, 150.0), "talking")
        self.assertEqual(clipper.resolve_mode("gameplay", words, 150.0), "gameplay")


class TalkingScoreTest(unittest.TestCase):
    def _segments(self):
        segs = [
            {"start": 0.5, "end": 4.0, "text": HOOK_TEXT},
            {"start": 5.0, "end": 9.0, "text": Q_TEXT + "?"},
            {"start": 10.0, "end": 14.0, "text": STORY_TEXT},
        ]
        words = []
        for s in segs:
            words += _words_from(s["text"], start=s["start"], step=0.35)
        return words, segs

    def test_hook_and_question_window_beats_flat_window(self):
        words, segs = self._segments()
        hook_score, hook_reasons = clipper._talking_window(0, 30, words, segs)
        flat_words = _words_from(FLAT_TEXT, start=0.5, step=0.9)
        flat_segs = [{"start": 0.5, "end": 25.0, "text": FLAT_TEXT}]
        flat_score, _ = clipper._talking_window(0, 30, flat_words, flat_segs)
        self.assertGreater(hook_score, flat_score)
        self.assertGreater(hook_score, 0.3)
        self.assertIn("strong opening line", "; ".join(hook_reasons))

    def test_window_with_no_speech_scores_zero(self):
        score, reasons = clipper._talking_window(0, 30, [], [])
        self.assertEqual(score, 0.0)
        self.assertIn("no speech", reasons[0])

    def test_draft_copy_is_generic(self):
        w = {"excerpt": "here is the thing about pricing", "start": 0.0,
             "end": 30.0, "reason": "strong opening line"}
        title, desc, tags = clipper.draft_copy(w, 1, "talking")
        self.assertNotIn("GTA", title)
        self.assertNotIn("GTA", desc)


class ClipperGateTest(unittest.TestCase):
    def test_refuses_without_i_own_it(self):
        argv = ["clipper.py", "input/some-video.mp4", "--mode", "talking"]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as ctx:
                clipper.main()
        self.assertEqual(ctx.exception.code, 2)

    def test_with_flag_it_gets_past_the_gate(self):
        # With the flag, the job proceeds far enough to complain about
        # the (nonexistent) input file instead of the attestation.
        tmp = Path(tempfile.mkdtemp())
        try:
            argv = ["clipper.py", "--i-own-it", "input/does-not-exist.mp4",
                    "--out", str(tmp / "job-x")]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as ctx:
                    clipper.main()
            self.assertIn("input not found", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ServerJobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = (server.ROOT, server.OUT, server.INPUT)
        server.ROOT = self.tmp
        server.OUT = self.tmp / "output"
        server.INPUT = self.tmp / "input"
        server.OUT.mkdir(parents=True)
        server.INPUT.mkdir(parents=True)
        self.cmds = []

        def fake_run(job_dir, cmd):
            self.cmds.append(cmd)
            job_dir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "job": job_dir.name, "created_utc": "20261118-000000",
                "source": "test", "owned": True,
                "attested_at": "2026-11-18T00:00:00Z",
                "mode_requested": "auto", "mode": "gameplay", "clips": [
                    {"id": "clip-01", "job": job_dir.name, "rank": 1,
                     "start": 0, "end": 30, "duration": 30, "score": 0.5,
                     "video": "clip-01.mp4", "status": "pending",
                     "draft_title": "t", "draft_description": "d"}]}
            (job_dir / "clips.json").write_text(json.dumps(manifest))
            (job_dir / "clip-01.mp4").write_bytes(b"\x00" * 64)
            server._write_status(job_dir, "done")

        self.run_patch = mock.patch.object(server, "_run_clipper_job",
                                           side_effect=fake_run)
        self.run_patch.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.run_patch.stop()
        server.ROOT, server.OUT, server.INPUT = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post_json(self, path, obj):
        import urllib.request
        req = urllib.request.Request(
            self._url(path), data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get_json(self, path):
        import urllib.request
        with urllib.request.urlopen(self._url(path), timeout=10) as r:
            return json.loads(r.read())

    def test_json_job_refused_without_attestation(self):
        status, body = self._post_json("/api/jobs", {
            "url": "https://example.com/video.mp4", "mode": "auto",
            "owned": False})
        self.assertEqual(status, 400)
        self.assertIn("ownership", body["error"])
        self.assertEqual(self.cmds, [])  # clipper never invoked

    def test_json_job_starts_with_attestation(self):
        status, body = self._post_json("/api/jobs", {
            "url": "https://example.com/video.mp4", "mode": "auto",
            "model": "tiny", "owned": True})
        self.assertEqual(status, 202)
        job_id = body["job"]
        for _ in range(50):
            jobs = self._get_json("/api/jobs")["jobs"]
            if jobs and jobs[0]["status"] == "done":
                break
            import time
            time.sleep(0.1)
        self.assertEqual(jobs[0]["job"], job_id)
        self.assertTrue(jobs[0]["owned"])
        self.assertEqual(jobs[0]["clip_count"], 1)
        self.assertIn("--i-own-it", self.cmds[0])
        self.assertIn("--mode", self.cmds[0])

    def test_multipart_upload_refused_without_attestation(self):
        import urllib.request
        boundary = "----testboundary"
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="mode"\r\n\r\n'
                "auto\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="vid.mp4"\r\n'
                "Content-Type: video/mp4\r\n\r\n").encode() + b"\x00" * 128 + \
            f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self._url("/api/jobs"), data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
            self.assertIn("ownership", json.loads(e.read())["error"])
        self.assertEqual(self.cmds, [])

    def test_multipart_upload_starts_with_attestation(self):
        import urllib.request
        boundary = "----testboundary"
        body = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="owned"\r\n\r\n'
                "true\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="mode"\r\n\r\n'
                "talking\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="vid.mp4"\r\n'
                "Content-Type: video/mp4\r\n\r\n").encode() + b"\x00" * 128 + \
            f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self._url("/api/jobs"), data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 202)
            body_json = json.loads(r.read())
        self.assertTrue((server.INPUT / "vid.mp4").exists())
        self.assertIn("job-", body_json["job"])


class DownloadUrlTest(unittest.TestCase):
    def test_download_url_picks_its_own_file(self):
        # Regression: download_url used to pick the newest file in
        # input/ — an upload landing seconds earlier could make it clip
        # the wrong video entirely. It must return the file yt-dlp
        # actually wrote, even when a newer unrelated video sits there.
        tmp = Path(tempfile.mkdtemp())
        try:
            other = tmp / "other.mp4"
            other.write_bytes(b"\x00" * 100)
            os.utime(other, (2000000000, 2000000000))  # far-future mtime

            def fake_run(cmd, check=True):
                dl = next(tmp.glob("dl-*"))
                (dl / "Downloaded Video.mp4").write_bytes(b"\x00" * 200)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(clipper, "run", side_effect=fake_run):
                got = clipper.download_url("https://example.com/v.mp4", tmp)
            self.assertEqual(got.name, "Downloaded Video.mp4")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FindClipByJobTest(unittest.TestCase):
    """Clip ids repeat across jobs (every job has a clip-01) — a job
    qualifier must pick the right one, and a wrong job must miss."""

    def test_job_qualifies_clip_id(self):
        tmp = Path(tempfile.mkdtemp())
        orig_out = server.OUT
        server.OUT = tmp
        try:
            for name in ("job-a", "job-b"):
                d = tmp / name
                d.mkdir()
                manifest = {"job": name, "created_utc": "x", "source": "s",
                            "owned": True, "clips": [
                                {"id": "clip-01", "job": name, "rank": 1,
                                 "start": 0, "end": 30, "duration": 30,
                                 "score": 0.5, "video": "clip-01.mp4",
                                 "status": "pending", "draft_title": name,
                                 "draft_description": ""}]}
                (d / "clips.json").write_text(json.dumps(manifest))
            job, clip = server.find_clip("clip-01", "job-a")
            self.assertEqual(job["job"], "job-a")
            self.assertEqual(clip["draft_title"], "job-a")
            job2, _ = server.find_clip("clip-01", "job-missing")
            self.assertIsNone(job2)
        finally:
            server.OUT = orig_out
            shutil.rmtree(tmp, ignore_errors=True)


class GateOwnedTest(unittest.TestCase):
    def test_post_refused_without_ownership_attestation(self):
        import posters.gate as gate
        tmp = Path(tempfile.mkdtemp())
        orig_out = gate.OUT
        gate.OUT = tmp
        try:
            job_dir = tmp / "job-noattest"
            job_dir.mkdir()
            manifest = {
                "job": "job-noattest", "created_utc": "x",
                "source": "input/x.mp4", "clips": [
                    {"id": "clip-01", "job": "job-noattest", "rank": 1,
                     "start": 0, "end": 30, "duration": 30, "score": 0.9,
                     "video": "clip-01.mp4", "draft_title": "t",
                     "draft_description": "d", "status": "approved"}]}
            (job_dir / "clips.json").write_text(json.dumps(manifest))
            (job_dir / "clip-01.mp4").write_bytes(b"\x00" * 64)
            with self.assertRaises(gate.GateError) as ctx:
                gate.post_clip("clip-01", ["youtube"], dry_run=True)
            self.assertIn("ownership", str(ctx.exception))
        finally:
            gate.OUT = orig_out
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
