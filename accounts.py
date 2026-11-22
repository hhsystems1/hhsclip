#!/usr/bin/env python3
"""HHS Clips — accounts + sessions. Stdlib only.

Sign-in is OFF until the owner creates the first account: when no
accounts file exists (or it has no users), the server behaves exactly
as before — no login, local use unchanged. The moment the first
account exists, every page and API route requires a signed-in
session, except the login page itself and the static review files
(review.html, review/demo/, index.html — those stay public).

Passwords are PBKDF2-HMAC-SHA256 hashes (200k rounds, per-user salt);
nothing plaintext is ever written to disk. Sessions are HMAC-signed
tokens in an HttpOnly cookie, good for 30 days.

Accounts live in accounts.json (git-ignored, never committed).
Manage them with the CLI at the bottom of this file:

    python3 accounts.py add <username> <owner|reviewer> [client]
    python3 accounts.py list
    python3 accounts.py passwd <username>
    python3 accounts.py remove <username>

A reviewer account carries a `client` slug (e.g. tech4health) and
only ever sees jobs tagged with that client. An owner sees
everything. Usernames are never hardcoded — the owner creates
accounts by name at setup time.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from base64 import urlsafe_b64encode, urlsafe_b64decode
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACCOUNTS_FILE = "accounts.json"
SECRET_FILE = ".session-secret"
SESSION_COOKIE = "hhsclip_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
PBKDF2_ROUNDS = 200_000

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ROLES = ("owner", "reviewer")


def accounts_path(root=None):
    return Path(root or ROOT) / ACCOUNTS_FILE


def secret_path(root=None):
    return Path(root or ROOT) / SECRET_FILE


def utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# -- accounts file ---------------------------------------------------
def load_users(root=None):
    p = accounts_path(root)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        users = data.get("users", [])
        return users if isinstance(users, list) else []
    return []


def auth_enabled(root=None):
    """Sign-in is on exactly when at least one account exists."""
    return bool(load_users(root))


def save_users(users, root=None):
    p = accounts_path(root)
    p.write_text(json.dumps({"users": users}, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def public_user(user):
    return {"username": user["username"], "role": user["role"],
            "client": user.get("client")}


# -- passwords -------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()


def verify_password(password, user):
    try:
        salt = bytes.fromhex(user["salt"])
        _, digest = hash_password(password, salt)
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, user["hash"])


def find_user(username, root=None):
    for u in load_users(root):
        if u["username"] == username:
            return u
    return None


def authenticate(username, password, root=None):
    user = find_user(username, root)
    if user is None or not verify_password(password, user):
        return None
    return user


def create_user(username, role, client=None, password=None, root=None):
    """Add one account. Raises ValueError on a bad name/role/client
    or a duplicate username. Returns the public user record."""
    if not USERNAME_RE.match(username or ""):
        raise ValueError("username must be 1-64 letters, digits, . _ -")
    if role not in ROLES:
        raise ValueError("role must be owner or reviewer")
    if role == "reviewer":
        if not client or not CLIENT_RE.match(client):
            raise ValueError("a reviewer needs a client slug "
                             "(lowercase letters, digits, dashes)")
    elif client and not CLIENT_RE.match(client):
        raise ValueError("client slug must be lowercase letters, "
                         "digits, dashes")
    if password is None or len(password) < 4:
        raise ValueError("password must be at least 4 characters")
    users = load_users(root)
    if any(u["username"] == username for u in users):
        raise ValueError(f"username {username!r} already exists")
    salt, digest = hash_password(password)
    user = {"username": username, "role": role, "client": client or None,
            "salt": salt, "hash": digest, "created_utc": utc_now()}
    users.append(user)
    save_users(users, root)
    return public_user(user)


def set_password(username, password, root=None):
    users = load_users(root)
    for u in users:
        if u["username"] == username:
            if len(password) < 4:
                raise ValueError("password must be at least 4 characters")
            u["salt"], u["hash"] = hash_password(password)
            save_users(users, root)
            return public_user(u)
    raise ValueError(f"no such user {username!r}")


def remove_user(username, root=None):
    users = load_users(root)
    kept = [u for u in users if u["username"] != username]
    if len(kept) == len(users):
        raise ValueError(f"no such user {username!r}")
    save_users(kept, root)
    return len(kept)


# -- sessions --------------------------------------------------------
def _get_secret(root=None):
    env = os.environ.get("HHS_SESSION_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    p = secret_path(root)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip().encode("utf-8")
        except OSError:
            pass
    secret = secrets.token_hex(32)
    p.write_text(secret, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return secret.encode("utf-8")


def _b64(data):
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(s):
    return urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session(username, root=None):
    payload = json.dumps(
        {"u": username, "exp": int(time.time()) + SESSION_TTL_SECONDS},
        separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_get_secret(root), payload, hashlib.sha256).digest()
    return f"v1.{_b64(payload)}.{_b64(sig)}"


def verify_session(token, root=None):
    """The signed-in user for a session cookie value, or None."""
    if not token:
        return None
    try:
        ver, payload_b64, sig_b64 = token.split(".")
        if ver != "v1":
            return None
        payload = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except (ValueError, TypeError):
        return None
    expect = hmac.new(_get_secret(root), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if int(data.get("exp", 0)) <= int(time.time()):
        return None
    return find_user(data.get("u", ""), root)


# -- CLI -------------------------------------------------------------
def _prompt_password(confirm=True):
    pw = getpass.getpass("Password: ")
    if confirm:
        pw2 = getpass.getpass("Password again: ")
        if pw != pw2:
            print("Passwords do not match.")
            sys.exit(2)
    return pw


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Manage HHS Clips accounts. Sign-in stays off "
                    "until the first account exists.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="add an account")
    p_add.add_argument("username")
    p_add.add_argument("role", choices=ROLES)
    p_add.add_argument("client", nargs="?", default=None,
                       help="client slug — required for reviewers")
    sub.add_parser("list", help="list accounts (no passwords shown)")
    p_pw = sub.add_parser("passwd", help="reset an account's password")
    p_pw.add_argument("username")
    p_rm = sub.add_parser("remove", help="remove an account")
    p_rm.add_argument("username")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "add":
            pw = _prompt_password()
            user = create_user(args.username, args.role, args.client, pw)
            print(f"Added {user['username']} ({user['role']}"
                  + (f", client {user['client']}" if user["client"] else "")
                  + ").")
            if auth_enabled():
                print("Sign-in is now ON — the app requires a login.")
        elif args.cmd == "list":
            users = load_users()
            if not users:
                print("No accounts yet — sign-in is OFF.")
            for u in users:
                print(f"{u['username']}  {u['role']}"
                      + (f"  client {u['client']}" if u.get("client") else "")
                      + f"  created {u.get('created_utc', '')}")
        elif args.cmd == "passwd":
            pw = _prompt_password()
            set_password(args.username, pw)
            print(f"Password reset for {args.username}.")
        elif args.cmd == "remove":
            left = remove_user(args.username)
            print(f"Removed {args.username}. {left} account(s) left.")
            if left == 0:
                print("No accounts left — sign-in is now OFF.")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
