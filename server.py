#!/usr/bin/env python3
"""
HHS Clips — standalone app server (v1). Stdlib only, no DB beyond
clips.json files under output/.

Views (dashboard.html): New (upload a file or paste a link),
Jobs (every job with mode/clips/status), Queue (approve / reject /
edit / post clips).

Rules carried over from slices 1+2:
- Nothing posts without an approval tap on an approved clip.
- Posting is dry-run by default; live needs {"live": true} + .env.
- Own content only: a job cannot be created without an ownership
  attestation (checkbox in the UI, --i-own-it on the CLI). The
  attestation is stored in the job manifest (owned + attested_at),
  and the posting gate refuses jobs without it.
- Single-tenant local app in v1: no accounts, no multi-user. Bind to
  localhost (127.0.0.1) for local-only use, or 0.0.0.0 for your own
  LAN (same Wi-Fi). Client sign-in (accounts.py) is a later slice:
  while no accounts exist the app behaves exactly as above, no login.
  Once the owner creates the first account, sign-in is required for
  every page and API route except the login page and the static
  review files — owners see everything, reviewers see only jobs
  tagged with their client.
"""
import accounts
import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
INPUT = ROOT / "input"

ALLOWED_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
MODES = ("gameplay", "talking", "auto")
MODELS = ("tiny", "small", "base")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status_path(job_dir):
    return job_dir / "_status.json"


def _write_status(job_dir, status, error=None, **extra):
    data = _read_status(job_dir)  # keep source/mode/owned/created_utc
    data.update({"status": status, "updated_utc": utc_now(), "error": error})
    data.update(extra)
    _status_path(job_dir).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_status(job_dir):
    p = _status_path(job_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def build_review_json(job):
    """review.json for a client review page: the clips plus, when the
    server has them, the hhs-core2 ingest endpoint + per-site token
    (env HHS_REVIEW_INGEST_URL / HHS_REVIEW_INGEST_TOKEN). Decisions
    POST to that endpoint as cta_click events with the decision in
    meta — see review.html and README.md. No ingest config means the
    review page keeps decisions in the browser only."""
    ingest = None
    url = os.environ.get("HHS_REVIEW_INGEST_URL", "").strip()
    token = os.environ.get("HHS_REVIEW_INGEST_TOKEN", "").strip()
    if url and token:
        ingest = {"url": url, "token": token}
    clips = []
    for c in job.get("clips", []):
        clips.append({
            "id": c["id"],
            "title": c.get("draft_title", c["id"]),
            "video": c.get("video", ""),
            "start": c.get("start"),
            "end": c.get("end"),
            "duration": c.get("duration"),
            "score": c.get("score"),
            "reason": c.get("reason", ""),
            "excerpt": c.get("excerpt", ""),
            "status": c.get("status", "pending"),
        })
    src = (job.get("source") or "").split("/")[-1] or job["job"]
    out = {
        "job": job["job"],
        "title": f"Review clips — {src}",
        "source": src,
        "created_utc": job.get("created_utc", ""),
        "mode": job.get("mode", ""),
        "clips": clips,
    }
    if ingest:
        out["ingest"] = ingest
    return out


def load_jobs():
    """All jobs, newest first. A job with clips.json is done (or errored
    after producing one); a job with only _status.json is running/queued."""
    jobs = []
    if not OUT.exists():
        return jobs
    for job_dir in sorted(OUT.iterdir(), reverse=True):
        if not job_dir.is_dir():
            continue
        mf = job_dir / "clips.json"
        st = _read_status(job_dir)
        if mf.exists():
            try:
                job = json.loads(mf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            job["_status"] = st.get("status", "done")
            job["_error"] = st.get("error")
            jobs.append(job)
        elif st:
            jobs.append({
                "job": job_dir.name,
                "created_utc": st.get("created_utc", ""),
                "source": st.get("source", ""),
                "mode": st.get("mode", ""),
                "mode_requested": st.get("mode", ""),
                "owned": st.get("owned", False),
                "client": st.get("client"),
                "clips": [],
                "_status": st.get("status", "running"),
                "_error": st.get("error"),
            })
    return jobs


def save_job(job):
    mf = OUT / job["job"] / "clips.json"
    mf.write_text(json.dumps(job, indent=2), encoding="utf-8")


def _visible_jobs(user, jobs):
    """Jobs this user may see. Owners see everything; a reviewer
    sees only jobs tagged with their client slug."""
    if user["role"] == "owner":
        return jobs
    return [j for j in jobs if j.get("client") == user.get("client")]


def _job_visible(user, job):
    return user["role"] == "owner" or job.get("client") == user.get("client")


def _find_clip_for_user(clip_id, job_name, user):
    """find_clip scoped to what this user may see: a reviewer only
    ever finds clips in their own client's jobs. Returns (job, clip)
    or (None, None)."""
    jobs = _visible_jobs(user, load_jobs())
    for job in jobs:
        if job_name and job["job"] != job_name:
            continue
        for clip in job["clips"]:
            if clip["id"] == clip_id:
                return job, clip
    if job_name:
        return None, None
    for job in jobs:
        for clip in job["clips"]:
            if clip["id"] == clip_id:
                return job, clip
    return None, None


def set_job_client(job_id, client):
    """Tag a job to a client slug ('' clears the tag). Stored in the
    job manifest (clips.json) and, for running jobs, in _status.json.
    Returns the job dict, or None when the job does not exist."""
    for job in load_jobs():
        if job["job"] != job_id:
            continue
        job["client"] = client or None
        if (OUT / job_id / "clips.json").exists():
            save_job(job)
        st = _read_status(OUT / job_id)
        if st:
            _write_status(OUT / job_id, st.get("status", "running"),
                          client=client or None)
        return job
    return None


def find_clip(clip_id, job_name=None):
    # Clip ids repeat across jobs (every job has a clip-01), so an
    # exact (job, id) match wins; id-alone is only a fallback.
    for job in load_jobs():
        if job_name and job["job"] != job_name:
            continue
        for clip in job["clips"]:
            if clip["id"] == clip_id:
                return job, clip
    if job_name:
        return None, None
    for job in load_jobs():
        for clip in job["clips"]:
            if clip["id"] == clip_id:
                return job, clip
    return None, None


def _clipper_python():
    venv_py = ROOT / "venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


def _run_clipper_job(job_dir, cmd):
    """Run clipper.py for one job in a background thread. Status and a
    log tail live in the job dir so the UI can poll them."""
    job_dir.mkdir(parents=True, exist_ok=True)
    _write_status(job_dir, "running")
    log_path = job_dir / "_clipper.log"
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            r = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                               text=True, timeout=7200)
        if r.returncode == 0 and (job_dir / "clips.json").exists():
            _write_status(job_dir, "done")
        else:
            _write_status(job_dir, "error",
                          f"clipper exited {r.returncode} — see the job log")
    except Exception as e:  # noqa: BLE001 — report it in the job status
        _write_status(job_dir, "error", f"{type(e).__name__}: {e}")


def _safe_filename(name):
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name or len(name) > 200:
        return None
    if Path(name).suffix.lower() not in ALLOWED_EXTS:
        return None
    return name


def _parse_multipart(body, boundary):
    """Minimal multipart/form-data parser (stdlib only). Returns
    (fields: {name: str}, files: {name: (filename, bytes)})."""
    fields, files = {}, {}
    sep = b"--" + boundary
    for part in body.split(sep):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if data.endswith(b"--"):
            data = data[:-2]
        headers = {}
        for line in head.decode(errors="replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        m = re.search(r'name="([^"]*)"', headers.get("content-disposition", ""))
        if not m:
            continue
        name = m.group(1)
        fm = re.search(r'filename="([^"]*)"',
                        headers.get("content-disposition", ""))
        if fm and fm.group(1):
            files[name] = (fm.group(1), data)
        else:
            fields[name] = data.decode(errors="replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # -- auth --------------------------------------------------------
    def _session_user(self):
        """Signed-in user for this request, or None. Reads the
        HttpOnly session cookie and verifies its HMAC signature."""
        token = None
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith(accounts.SESSION_COOKIE + "="):
                token = part[len(accounts.SESSION_COOKIE) + 1:]
                break
        if not token:
            return None
        return accounts.verify_session(token, ROOT)

    def _require_auth(self, page=True):
        """Enforce sign-in when it is switched on. Returns the user
        dict, or None after sending the refusal (302 to /login for
        pages, 401 JSON for API calls). When auth is OFF (no accounts
        file), returns a local-owner stand-in so the app behaves
        exactly as before — no login, full access."""
        if not accounts.auth_enabled(ROOT):
            return {"username": None, "role": "owner", "client": None,
                    "_local": True}
        user = self._session_user()
        if user is None:
            if page:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json({"error": "sign-in required"}, 401)
            return None
        return user

    def _require_owner(self, page=False):
        """Like _require_auth, but reviewers are refused (403)."""
        user = self._require_auth(page=page)
        if user is None:
            return None
        if user["role"] != "owner":
            if page:
                self.send_error(403, "owner only")
            else:
                self._json({"error": "owner only"}, 403)
            return None
        return user

    def _serve_file(self, path, ctype="text/html; charset=utf-8"):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_public_static(self, rel):
        """Serve a static review file without sign-in (review.html,
        review/demo/*). Path traversal is refused."""
        f = (ROOT / rel).resolve()
        if not str(f).startswith(str(ROOT.resolve())) or not f.exists():
            self.send_error(404, "not found")
            return
        ctype, _ = mimetypes.guess_type(str(f))
        self._serve_file(f, ctype or "application/octet-stream")

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    # -- GET ---------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/login":
            # Public. Already signed in? Go where you belong.
            user = self._session_user()
            if user is not None and accounts.auth_enabled(ROOT):
                self.send_response(302)
                self.send_header(
                    "Location",
                    "/review" if user["role"] == "reviewer" else "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._serve_file(ROOT / "login.html")
            return
        if u.path == "/review":
            # The live reviewer page: only reviewers land here.
            user = self._require_auth(page=True)
            if user is None:
                return
            if user["role"] != "reviewer":
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._serve_file(ROOT / "reviewer.html")
            return
        if u.path == "/index.html":
            # Public front door — the same file Netlify serves at the
            # root. (The dashboard lives at / and is sign-in gated.)
            self._serve_file(ROOT / "index.html")
            return
        if u.path == "/review.html":
            self._serve_public_static("review.html")
            return
        if u.path.startswith("/review/demo/"):
            self._serve_public_static(u.path.lstrip("/"))
            return
        if u.path == "/api/me":
            if not accounts.auth_enabled(ROOT):
                self._json({"auth": False, "user": None})
                return
            user = self._session_user()
            if user is None:
                self._json({"error": "sign-in required"}, 401)
                return
            self._json({"auth": True, "user": accounts.public_user(user)})
            return
        if u.path in ("/",):
            user = self._require_auth(page=True)
            if user is None:
                return
            if user["role"] == "reviewer":
                # Reviewers never see the owner dashboard.
                self.send_response(302)
                self.send_header("Location", "/review")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._serve_file(ROOT / "dashboard.html")
            return
        if u.path == "/api/jobs":
            user = self._require_auth(page=False)
            if user is None:
                return
            out = []
            for job in _visible_jobs(user, load_jobs()):
                clips = job.get("clips", [])
                out.append({
                    "job": job["job"],
                    "created_utc": job.get("created_utc", ""),
                    "source": job.get("source", ""),
                    "mode": job.get("mode", ""),
                    "mode_requested": job.get("mode_requested", ""),
                    "owned": bool(job.get("owned")),
                    "client": job.get("client"),
                    "status": job.get("_status", "done"),
                    "error": job.get("_error"),
                    "clip_count": len(clips),
                    "approved": sum(1 for c in clips if c.get("status") == "approved"),
                    "rejected": sum(1 for c in clips if c.get("status") == "rejected"),
                    "pending": sum(1 for c in clips if c.get("status") == "pending"),
                })
            self._json({"jobs": out})
            return
        m = re.match(r"^/api/jobs/([A-Za-z0-9._-]+)/log$", u.path)
        if m:
            user = self._require_auth(page=False)
            if user is None:
                return
            job = next((j for j in load_jobs() if j["job"] == m.group(1)), None)
            if job is None or not _job_visible(user, job):
                self.send_error(404, "no log yet")
                return
            f = OUT / m.group(1) / "_clipper.log"
            if not f.exists():
                self.send_error(404, "no log yet")
                return
            text = f.read_text(encoding="utf-8", errors="replace")[-4000:]
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.match(r"^/api/jobs/([A-Za-z0-9._-]+)$", u.path)
        if m:
            user = self._require_auth(page=False)
            if user is None:
                return
            for job in load_jobs():
                if job["job"] == m.group(1):
                    if not _job_visible(user, job):
                        break
                    job2 = dict(job)
                    job2["status"] = job.get("_status", "done")
                    job2["error"] = job.get("_error")
                    self._json(job2)
                    return
            self._json({"error": "job not found"}, 404)
            return
        if u.path == "/api/clips":
            user = self._require_auth(page=False)
            if user is None:
                return
            clips = []
            visible = _visible_jobs(user, load_jobs())
            for job in visible:
                for c in job["clips"]:
                    c2 = dict(c)
                    c2["job_created"] = job.get("created_utc", "")
                    c2["source"] = job.get("source", "")
                    c2["mode"] = job.get("mode", "")
                    c2["owned"] = bool(job.get("owned"))
                    c2["client"] = job.get("client")
                    c2["video_url"] = f"/files/{job['job']}/{c['video']}"
                    clips.append(c2)
            self._json({"jobs": len(visible), "clips": clips})
            return
        if u.path == "/api/posting/config":
            # Names + booleans only — never any secret value.
            user = self._require_owner(page=False)
            if user is None:
                return
            from posters.gate import configured_map
            self._json({"dry_run_default": True,
                        "platforms": configured_map()})
            return
        m = re.match(r"^/files/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$", unquote(u.path))
        if m:
            user = self._require_auth(page=False)
            if user is None:
                return
            job_dir, fname = m.group(1), m.group(2)
            job = next((j for j in load_jobs() if j["job"] == job_dir), None)
            if job is None or not _job_visible(user, job):
                self.send_error(404, "not found")
                return
            f = OUT / job_dir / fname
            # stay inside output/
            if not str(f.resolve()).startswith(str(OUT.resolve())) or not f.exists():
                self.send_error(404, "not found")
                return
            size = f.stat().st_size
            ctype, _ = mimetypes.guess_type(str(f))
            # Range support so phone video players can seek
            range_hdr = self.headers.get("Range")
            start, end = 0, size - 1
            status = 200
            if range_hdr:
                mm = re.match(r"bytes=(\d*)-(\d*)$", range_hdr.strip())
                if mm:
                    if mm.group(1):
                        start = int(mm.group(1))
                    if mm.group(2):
                        end = int(mm.group(2))
                    end = min(end, size - 1)
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return
        self.send_error(404, "not found")

    # -- POST --------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/login":
            self._login()
            return
        if u.path == "/api/logout":
            self._logout()
            return
        user = self._require_auth(page=False)
        if user is None:
            return
        if u.path == "/api/jobs":
            if user["role"] != "owner":
                self._json({"error": "owner only"}, 403)
                return
            self._create_job()
            return
        m = re.match(r"^/api/jobs/([A-Za-z0-9._-]+)/export-review$", u.path)
        if m:
            if user["role"] != "owner":
                self._json({"error": "owner only"}, 403)
                return
            self._export_review(m.group(1))
            return
        m = re.match(r"^/api/jobs/([A-Za-z0-9._-]+)/client$", u.path)
        if m:
            if user["role"] != "owner":
                self._json({"error": "owner only"}, 403)
                return
            try:
                body = json.loads(self._read_body() or b"{}")
            except Exception:
                self._json({"error": "bad JSON"}, 400)
                return
            client = str(body.get("client") or "").strip()
            if client and not accounts.CLIENT_RE.match(client):
                self._json({"error": "client must be a lowercase slug "
                                     "(letters, digits, dashes)"}, 400)
                return
            job = set_job_client(m.group(1), client)
            if job is None:
                self._json({"error": "job not found"}, 404)
                return
            self._json({"ok": True, "job": job["job"],
                        "client": job.get("client")})
            return
        m = re.match(r"^/api/clips/([A-Za-z0-9._-]+)/(status|edit|post)$", u.path)
        if not m:
            self.send_error(404, "not found")
            return
        clip_id, action = m.group(1), m.group(2)
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "bad JSON"}, 400)
            return
        if action in ("edit", "post") and user["role"] != "owner":
            # Reviewers approve / ask for changes only — they never
            # edit titles or post.
            self._json({"error": "owner only"}, 403)
            return
        if action == "post":
            # Live only when the caller explicitly asks: {"live": true}.
            # Anything else is a dry run — zero network calls.
            from posters.gate import GateError, post_clip
            platforms = body.get("platforms") or ["all"]
            if isinstance(platforms, str):
                platforms = [p.strip() for p in platforms.split(",") if p.strip()]
            live = body.get("live") is True
            try:
                receipts = post_clip(clip_id, platforms, dry_run=not live,
                                     job_name=body.get("job"))
            except GateError as e:
                self._json({"error": str(e)}, 403)
                return
            self._json({"ok": True, "id": clip_id, "live": live,
                        "receipts": receipts})
            return
        job, clip = _find_clip_for_user(clip_id, body.get("job"), user)
        if clip is None:
            self._json({"error": "clip not found"}, 404)
            return
        if action == "status":
            st = body.get("status")
            if st not in ("pending", "approved", "rejected"):
                self._json({"error": "status must be pending/approved/rejected"}, 400)
                return
            clip["status"] = st
            clip["reviewed_by"] = user.get("username")
            clip["reviewed_at"] = utc_now()
            if st == "approved":
                # A fresh approval clears any earlier change request.
                clip["review_reason"] = ""
                clip["review_note"] = ""
            else:
                # A reason + note is how a reviewer asks for changes.
                # (A plain reject from the owner dashboard may omit them.)
                if "reason" in body:
                    clip["review_reason"] = str(body["reason"])[:200]
                if "note" in body:
                    clip["review_note"] = str(body["note"])[:1000]
        else:
            if "draft_title" in body:
                clip["draft_title"] = str(body["draft_title"])[:200]
            if "draft_description" in body:
                clip["draft_description"] = str(body["draft_description"])[:1000]
        save_job(job)
        self._json({"ok": True, "id": clip_id, "status": clip["status"]})

    def _login(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "bad JSON"}, 400)
            return
        user = accounts.authenticate(str(body.get("username", "")).strip(),
                                     str(body.get("password", "")), ROOT)
        if user is None:
            # Generic on purpose — no telling a stranger which half
            # was wrong.
            self._json({"error": "wrong username or password"}, 401)
            return
        token = accounts.make_session(user["username"], ROOT)
        out = json.dumps({
            "ok": True, "user": accounts.public_user(user),
            "redirect": "/review" if user["role"] == "reviewer" else "/",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Set-Cookie",
            f"{accounts.SESSION_COOKIE}={token}; HttpOnly; "
            f"SameSite=Lax; Path=/; "
            f"Max-Age={accounts.SESSION_TTL_SECONDS}")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _logout(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Set-Cookie",
            f"{accounts.SESSION_COOKIE}=; HttpOnly; "
            "SameSite=Lax; Path=/; Max-Age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _export_review(self, job_id):
        """Export a job as a static client review page: review/<job-id>/
        with index.html (copy of review.html), review.json (clips +
        ingest config when set), and the clip mp4s. The folder is
        ready to drag onto Netlify Drop — no server, no posting."""
        job = None
        for j in load_jobs():
            if j["job"] == job_id:
                job = j
                break
        if job is None:
            self._json({"error": "job not found"}, 404)
            return
        if not job.get("clips"):
            self._json({"error": "job has no clips yet"}, 400)
            return
        src_html = ROOT / "review.html"
        if not src_html.exists():
            self._json({"error": "review.html missing from server dir"}, 500)
            return
        dest = ROOT / "review" / job_id
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_bytes(src_html.read_bytes())
        review = build_review_json(job)
        (dest / "review.json").write_text(json.dumps(review, indent=2),
                                          encoding="utf-8")
        copied, missing = [], []
        for c in job["clips"]:
            src = OUT / job_id / (c.get("video") or "")
            if src.exists():
                shutil.copyfile(src, dest / src.name)
                copied.append(src.name)
            else:
                missing.append(c.get("video") or "")
        self._json({
            "ok": True, "job": job_id, "dir": str(dest),
            "files": ["index.html", "review.json"] + copied,
            "missing": missing,
            "ingest_configured": bool(review.get("ingest")),
            "next": "Drag this folder onto Netlify Drop "
                    "(https://app.netlify.com/drop) or run: netlify deploy "
                    f"--dir {dest} — then send the link to your client.",
        })

    def _create_job(self):
        """Start a clip job from the UI. Own-content attestation is
        required: without it the job is refused (HTTP 400) and nothing
        is downloaded or cut."""
        ctype = self.headers.get("Content-Type", "")
        body = self._read_body()
        mode, model, owned, src, is_url = "auto", "tiny", False, None, False
        if ctype.startswith("multipart/form-data"):
            m = re.search(r"boundary=([^;]+)", ctype)
            if not m:
                self._json({"error": "bad multipart request"}, 400)
                return
            fields, files = _parse_multipart(body, m.group(1).strip().encode())
            mode = fields.get("mode", "auto").strip()
            model = fields.get("model", "tiny").strip()
            owned = fields.get("owned") in ("true", "on", "1")
            if "file" in files:
                fname, data = files["file"]
                safe = _safe_filename(fname)
                if not safe:
                    self._json({"error": "bad file name or format — use .mp4 / .mkv / .webm / .mov"}, 400)
                    return
                INPUT.mkdir(parents=True, exist_ok=True)
                dest = INPUT / safe
                dest.write_bytes(data)
                src, is_url = str(dest), False
            elif fields.get("url", "").strip():
                src, is_url = fields["url"].strip(), True
        elif ctype.startswith("application/json"):
            try:
                data = json.loads(body or b"{}")
            except Exception:
                self._json({"error": "bad JSON"}, 400)
                return
            mode = str(data.get("mode", "auto"))
            model = str(data.get("model", "tiny"))
            owned = data.get("owned") is True
            if str(data.get("url", "")).strip():
                src, is_url = str(data["url"]).strip(), True
        else:
            self._json({"error": "send multipart (file) or JSON (url)"}, 400)
            return
        if not owned:
            self._json({"error": "ownership attestation required — tick "
                                 "'I own this video or have rights to post "
                                 "clips from it' first. This app is for "
                                 "your own videos only."}, 400)
            return
        if mode not in MODES:
            self._json({"error": f"mode must be one of {', '.join(MODES)}"}, 400)
            return
        if model not in MODELS:
            self._json({"error": f"model must be one of {', '.join(MODELS)}"}, 400)
            return
        if not src:
            self._json({"error": "give a video file or a URL"}, 400)
            return
        if is_url and not src.startswith(("http://", "https://")):
            self._json({"error": "URL must start with http:// or https://"}, 400)
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        job_id = f"job-{stamp}"
        job_dir = OUT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        _write_status(job_dir, "running", source=src, mode=mode,
                      owned=True, created_utc=stamp)
        cmd = [_clipper_python(), "-u", str(ROOT / "clipper.py"),
               *(["--url", src] if is_url else [src]),
               "--i-own-it", "--mode", mode, "--model", model,
               "--out", str(job_dir)]
        threading.Thread(target=_run_clipper_job, args=(job_dir, cmd),
                         daemon=True).start()
        self._json({"ok": True, "job": job_id, "status": "running"}, 202)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 = this machine only (default). "
                         "0.0.0.0 = your LAN too, so your phone on the "
                         "same Wi-Fi can open it. Sign-in is off until "
                         "the owner creates the first account "
                         "(accounts.py add); then a login is required.")
    args = ap.parse_args()
    print(f"HHS Clips — http://{args.host}:{args.port}/")
    print("Own videos only: every job needs an ownership attestation.")
    print("Posting is approval-gated and dry-run by default.")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
