#!/usr/bin/env python3
"""
One-time YouTube OAuth helper — gets the refresh token that
posters/youtube.py needs. Run it once on the machine that will post.

  1. In Google Cloud Console, create OAuth client ID (Desktop app)
     for a NEW project, enable the YouTube Data API v3.
  2. Run:  python3 posters/youtube_oauth.py
     It prints an auth URL — open it, sign in to the YouTube channel,
     paste the code back here.
  3. It prints a refresh token. Paste THAT into .env as
     YOUTUBE_REFRESH_TOKEN (plus the client id/secret). The token is
     printed to your terminal only — this script writes nothing to disk.

Needs YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET in the environment
or .env first (or pass --client-id / --client-secret).
"""
import argparse
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from posters.common import http_json, load_dotenv  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", default=os.environ.get("YOUTUBE_CLIENT_ID"))
    ap.add_argument("--client-secret", default=os.environ.get("YOUTUBE_CLIENT_SECRET"))
    args = ap.parse_args()
    if not args.client_id or not args.client_secret:
        sys.exit("set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first (see .env.example)")
    params = {
        "client_id": args.client_id,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    print("Open this URL, sign in to the YouTube channel, allow the upload scope:")
    print(url)
    code = input("\nPaste the authorization code here: ").strip()
    data = http_json("POST", "https://oauth2.googleapis.com/token", {
        "code": code,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "authorization_code",
    })
    refresh = data.get("refresh_token")
    if not refresh:
        sys.exit(f"no refresh token returned: {str(data)[:300]}")
    print("\n--- paste this into .env (never commit it, never share it) ---")
    print(f"YOUTUBE_REFRESH_TOKEN={refresh}")


if __name__ == "__main__":
    main()
