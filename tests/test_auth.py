#!/usr/bin/env python3
"""HHS Clips auth tests — accounts, sessions, and per-client scoping.
Run:  python3 -m unittest discover -s tests -v

- Sign-in is OFF with no accounts file: the app behaves exactly as
  before (no login, full access).
- Once accounts exist: every page/API needs a session except /login
  and the static review files; wrong passwords fail; sessions are
  HMAC-signed and expire.
- A reviewer sees only jobs tagged with their client — other clients'
  jobs, clips, logs, and video files all 404; posting/editing/creating
  jobs and tagging clients are owner-only (403).
- Asking for changes stores the reason + note on the real clip.
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import accounts
import server


class AccountsUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_password_round_trip_and_wrong_password_fails(self):
        salt, digest = accounts.hash_password("correct-horse")
        user = {"salt": salt, "hash": digest}
        self.assertTrue(accounts.verify_password("correct-horse", user))
        self.assertFalse(accounts.verify_password("wrong-horse", user))
        self.assertFalse(accounts.verify_password("", user))

    def test_auth_off_with_no_file_then_on_after_first_account(self):
        self.assertFalse(accounts.auth_enabled(self.tmp))
        accounts.create_user("brendon", "owner", password="pw-owner",
                             root=self.tmp)
        self.assertTrue(accounts.auth_enabled(self.tmp))
        self.assertEqual(
            accounts.authenticate("brendon", "pw-owner", self.tmp)["role"],
            "owner")
        self.assertIsNone(
            accounts.authenticate("brendon", "nope", self.tmp))
        self.assertIsNone(
            accounts.authenticate("nobody", "pw-owner", self.tmp))

    def test_reviewer_needs_client_and_duplicates_rejected(self):
        with self.assertRaises(ValueError):
            accounts.create_user("shayne", "reviewer", password="pw-x",
                                 root=self.tmp)
        with self.assertRaises(ValueError):
            accounts.create_user("shayne", "reviewer", "Bad Slug!",
                                 password="pw-x", root=self.tmp)
        accounts.create_user("shayne", "reviewer", "tech4health",
                             password="pw-x", root=self.tmp)
        with self.assertRaises(ValueError):
            accounts.create_user("shayne", "reviewer", "tech4health",
                                 password="pw-y", root=self.tmp)
        with self.assertRaises(ValueError):
            accounts.create_user("bad name!", "owner", password="pw-x",
                                 root=self.tmp)

    def test_set_password_and_remove(self):
        accounts.create_user("shayne", "reviewer", "tech4health",
                             password="pw-old", root=self.tmp)
        accounts.set_password("shayne", "pw-new", root=self.tmp)
        self.assertIsNone(
            accounts.authenticate("shayne", "pw-old", self.tmp))
        self.assertIsNotNone(
            accounts.authenticate("shayne", "pw-new", self.tmp))
        left = accounts.remove_user("shayne", root=self.tmp)
        self.assertEqual(left, 0)
        self.assertFalse(accounts.auth_enabled(self.tmp))
        with self.assertRaises(ValueError):
            accounts.remove_user("shayne", root=self.tmp)

    def test_session_round_trip_tamper_and_expiry(self):
        os.environ["HHS_SESSION_SECRET"] = "unit-test-secret"
        try:
            accounts.create_user("brendon", "owner", password="pw-owner",
                                 root=self.tmp)
            token = accounts.make_session("brendon", self.tmp)
            user = accounts.verify_session(token, self.tmp)
            self.assertIsNotNone(user)
            self.assertEqual(user["username"], "brendon")
            # One changed character in the payload breaks the signature.
            parts = token.split(".")
            tampered = parts[0] + "." + parts[1][:-1] + (
                "A" if parts[1][-1] != "A" else "B") + "." + parts[2]
            self.assertIsNone(accounts.verify_session(tampered, self.tmp))
            self.assertIsNone(accounts.verify_session("garbage", self.tmp))
            self.assertIsNone(accounts.verify_session("", self.tmp))
            # An expired token (signed by hand with a past exp) fails.
            import base64
            import hashlib
            import hmac
            payload = json.dumps(
                {"u": "brendon", "exp": int(time.time()) - 10},
                separators=(",", ":")).encode()
            secret = accounts._get_secret(self.tmp)
            sig = hmac.new(secret, payload, hashlib.sha256).digest()
            b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
            expired = f"v1.{b64(payload)}.{b64(sig)}"
            self.assertIsNone(accounts.verify_session(expired, self.tmp))
        finally:
            os.environ.pop("HHS_SESSION_SECRET", None)

    def test_cli_add_and_list(self):
        argv = ["accounts.py", "add", "shayne", "reviewer", "tech4health"]
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(accounts.getpass, "getpass",
                                   side_effect=["pw-cli", "pw-cli"]):
                with mock.patch.object(accounts, "ROOT", self.tmp):
                    accounts.main()
        users = accounts.load_users(self.tmp)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["client"], "tech4health")
        self.assertNotIn("pw-cli", json.dumps(users))


class ServerAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = (server.ROOT, server.OUT, server.INPUT)
        server.ROOT = self.tmp
        server.OUT = self.tmp / "output"
        server.INPUT = self.tmp / "input"
        server.OUT.mkdir(parents=True)
        server.INPUT.mkdir(parents=True)
        # Minimal static pages the server serves (content is not
        # what these tests check — routing and status codes are).
        for name in ("dashboard.html", "login.html", "reviewer.html",
                     "index.html", "review.html"):
            (self.tmp / name).write_text(f"<html>{name}</html>")
        (self.tmp / "review" / "demo").mkdir(parents=True)
        (self.tmp / "review" / "demo" / "index.html").write_text(
            "<html>demo</html>")
        os.environ["HHS_SESSION_SECRET"] = "test-secret-for-auth"
        self._seed_job("job-a", "tech4health", clips=2)
        self._seed_job("job-b", "acme", clips=1)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.ROOT, server.OUT, server.INPUT = self.orig
        os.environ.pop("HHS_SESSION_SECRET", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_job(self, name, client, clips=1):
        d = self.tmp / "output" / name
        d.mkdir(parents=True, exist_ok=True)
        manifest = {"job": name, "created_utc": "20261119-000000",
                    "source": f"input/{name}.mp4", "owned": True,
                    "mode_requested": "auto", "mode": "talking",
                    "client": client, "clips": []}
        for i in range(1, clips + 1):
            manifest["clips"].append({
                "id": f"clip-0{i}", "job": name, "rank": i,
                "start": 0, "end": 30, "duration": 30, "score": 0.5,
                "video": f"clip-0{i}.mp4", "status": "pending",
                "draft_title": f"{name} clip {i}",
                "draft_description": ""})
            (d / f"clip-0{i}.mp4").write_bytes(b"\x00" * 64)
        (d / "clips.json").write_text(json.dumps(manifest))
        server._write_status(d, "done")

    def _raw(self, method, path, obj=None, cookie=None):
        body = json.dumps(obj).encode() if obj is not None else None
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = f"hhsclip_session={cookie}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                         timeout=10)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        out_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, out_headers, data

    def _json(self, data):
        return json.loads(data)

    def _make_accounts(self):
        accounts.create_user("brendon", "owner", password="pw-owner",
                             root=self.tmp)
        accounts.create_user("shayne", "reviewer", "tech4health",
                             password="pw-shayne", root=self.tmp)
        accounts.create_user("other", "reviewer", "acme",
                             password="pw-other", root=self.tmp)

    def _login(self, username, password):
        status, headers, data = self._raw(
            "POST", "/api/login", {"username": username, "password": password})
        cookie = None
        sc = headers.get("set-cookie", "")
        if sc.startswith("hhsclip_session="):
            cookie = sc.split(";", 1)[0].split("=", 1)[1]
        return status, self._json(data), cookie

    # -- no-accounts mode -------------------------------------------
    def test_no_accounts_mode_is_open(self):
        status, _, _ = self._raw("GET", "/")
        self.assertEqual(status, 200)
        status, _, data = self._raw("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(len(self._json(data)["jobs"]), 2)

    # -- login -------------------------------------------------------
    def test_unauthenticated_pages_redirect_and_api_401s(self):
        self._make_accounts()
        status, headers, _ = self._raw("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/login")
        status, headers, _ = self._raw("GET", "/review")
        self.assertEqual(status, 302)
        status, _, data = self._raw("GET", "/api/jobs")
        self.assertEqual(status, 401)
        self.assertIn("sign-in", self._json(data)["error"])

    def test_login_wrong_password_401_and_right_sets_cookie(self):
        self._make_accounts()
        status, body, cookie = self._login("shayne", "wrong")
        self.assertEqual(status, 401)
        self.assertIsNone(cookie)
        status, body, cookie = self._login("shayne", "pw-shayne")
        self.assertEqual(status, 200)
        self.assertEqual(body["redirect"], "/review")
        self.assertTrue(cookie)
        status, body, cookie = self._login("brendon", "pw-owner")
        self.assertEqual(status, 200)
        self.assertEqual(body["redirect"], "/")

    def test_tampered_cookie_rejected(self):
        self._make_accounts()
        _, _, cookie = self._login("shayne", "pw-shayne")
        bad = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
        status, _, _ = self._raw("GET", "/api/jobs", cookie=bad)
        self.assertEqual(status, 401)

    # -- public files stay public ------------------------------------
    def test_public_files_need_no_login(self):
        self._make_accounts()
        for path in ("/login", "/index.html", "/review.html",
                     "/review/demo/index.html"):
            status, _, _ = self._raw("GET", path)
            self.assertEqual(status, 200, path)

    # -- scoping ------------------------------------------------------
    def test_reviewer_sees_only_own_jobs(self):
        self._make_accounts()
        _, _, cookie = self._login("shayne", "pw-shayne")
        status, _, data = self._raw("GET", "/api/jobs", cookie=cookie)
        self.assertEqual(status, 200)
        jobs = self._json(data)["jobs"]
        self.assertEqual([j["job"] for j in jobs], ["job-a"])
        status, _, _ = self._raw("GET", "/api/jobs/job-b", cookie=cookie)
        self.assertEqual(status, 404)
        status, _, data = self._raw("GET", "/api/jobs/job-a", cookie=cookie)
        self.assertEqual(status, 200)
        status, _, data = self._raw("GET", "/api/clips", cookie=cookie)
        self.assertEqual(
            sorted(c["job"] for c in self._json(data)["clips"]),
            ["job-a", "job-a"])

    def test_owner_sees_all_jobs(self):
        self._make_accounts()
        _, _, cookie = self._login("brendon", "pw-owner")
        status, _, data = self._raw("GET", "/api/jobs", cookie=cookie)
        self.assertEqual(
            sorted(j["job"] for j in self._json(data)["jobs"]),
            ["job-a", "job-b"])

    def test_reviewer_root_redirects_to_review_page(self):
        self._make_accounts()
        _, _, cookie = self._login("shayne", "pw-shayne")
        status, headers, _ = self._raw("GET", "/", cookie=cookie)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/review")
        status, _, _ = self._raw("GET", "/review", cookie=cookie)
        self.assertEqual(status, 200)
        _, _, owner_cookie = self._login("brendon", "pw-owner")
        status, headers, _ = self._raw("GET", "/review", cookie=owner_cookie)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/")

    def test_reviewer_cannot_touch_other_client(self):
        self._make_accounts()
        _, _, cookie = self._login("shayne", "pw-shayne")
        # Other client's clip status, log, and video file: all 404.
        status, _, _ = self._raw("POST", "/api/clips/clip-01/status",
                                 {"status": "approved", "job": "job-b"},
                                 cookie=cookie)
        self.assertEqual(status, 404)
        status, _, _ = self._raw("GET", "/api/jobs/job-b/log", cookie=cookie)
        self.assertEqual(status, 404)
        status, _, _ = self._raw("GET", "/files/job-b/clip-01.mp4",
                                 cookie=cookie)
        self.assertEqual(status, 404)
        # Posting, editing, creating jobs, tagging clients: owner only.
        for method, path, obj in (
                ("POST", "/api/clips/clip-01/post",
                 {"platforms": ["youtube"], "live": False, "job": "job-a"}),
                ("POST", "/api/clips/clip-01/edit",
                 {"draft_title": "hijack", "job": "job-a"}),
                ("POST", "/api/jobs",
                 {"url": "https://example.com/v.mp4", "owned": True}),
                ("POST", "/api/jobs/job-a/client", {"client": "acme"})):
            status, _, _ = self._raw(method, path, obj, cookie=cookie)
            self.assertEqual(status, 403, path)

    def test_reviewer_approve_and_ask_for_changes_update_real_clip(self):
        self._make_accounts()
        _, _, cookie = self._login("shayne", "pw-shayne")
        status, _, data = self._raw(
            "POST", "/api/clips/clip-01/status",
            {"status": "approved", "job": "job-a"}, cookie=cookie)
        self.assertEqual(status, 200)
        manifest = json.loads(
            (self.tmp / "output" / "job-a" / "clips.json").read_text())
        clip = manifest["clips"][0]
        self.assertEqual(clip["status"], "approved")
        self.assertEqual(clip["reviewed_by"], "shayne")
        self.assertIn("reviewed_at", clip)
        status, _, data = self._raw(
            "POST", "/api/clips/clip-02/status",
            {"status": "rejected", "job": "job-a",
             "reason": "Caption is wrong",
             "note": "It says chimpanzees, he says chimpanzee."},
            cookie=cookie)
        self.assertEqual(status, 200)
        manifest = json.loads(
            (self.tmp / "output" / "job-a" / "clips.json").read_text())
        clip = manifest["clips"][1]
        self.assertEqual(clip["status"], "rejected")
        self.assertEqual(clip["review_reason"], "Caption is wrong")
        self.assertEqual(clip["review_note"],
                         "It says chimpanzees, he says chimpanzee.")

    def test_owner_can_tag_and_clear_client(self):
        self._make_accounts()
        _, _, cookie = self._login("brendon", "pw-owner")
        status, _, data = self._raw(
            "POST", "/api/jobs/job-a/client", {"client": "newclient"},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self._json(data)["client"], "newclient")
        manifest = json.loads(
            (self.tmp / "output" / "job-a" / "clips.json").read_text())
        self.assertEqual(manifest["client"], "newclient")
        status, _, data = self._raw(
            "POST", "/api/jobs/job-a/client", {"client": "Bad Slug!"},
            cookie=cookie)
        self.assertEqual(status, 400)
        status, _, data = self._raw(
            "POST", "/api/jobs/job-a/client", {"client": ""},
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNone(self._json(data)["client"])
        # A reviewer who lost their only job sees an empty list.
        _, _, shayne = self._login("shayne", "pw-shayne")
        status, _, data = self._raw("GET", "/api/jobs", cookie=shayne)
        self.assertEqual(self._json(data)["jobs"], [])

    def test_logout_clears_the_cookie(self):
        self._make_accounts()
        _, _, cookie = self._login("shayne", "pw-shayne")
        status, headers, _ = self._raw("POST", "/api/logout", {},
                                       cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers.get("set-cookie", ""))


if __name__ == "__main__":
    unittest.main()
