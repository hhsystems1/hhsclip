# GTA Clipper — Slice 2 Setup

**In plain language:** slice 1 cut the clips and queued them for approval.
Slice 2 can post an approved clip to YouTube, TikTok, Instagram and
Facebook — but only when you tap Post, and only after you set up one
developer app per platform below. **Nothing here is a paid service:**
every API in this file is free. What costs you is time in each
platform's review queue — and until those reviews pass, posts go out
private, by design.

**The one rule that matters:** use your **personal** accounts and a
**separate personal Meta app** for these GTA clips. Do NOT use the
Rivryn/HHS Meta app or HHS channels for this. Why: the Rivryn Meta
app (made Nov 16, still unconfigured) is the platform app for HHS
*client* workspaces — per-workspace client accounts, Meta App Review,
business verification. If your personal GTA clips went through it,
your clips and client accounts would share tokens, scopes and review
history, and a mistake on a clip could affect client publishing.
Same for YouTube and TikTok: your personal channel / creator account,
not HHS's. Keep the two worlds apart.

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored —
never commit it, never paste its values anywhere.

## 0. How posting works here (read first)

1. Approve a clip in the dashboard (slice 1).
2. On that approved clip, tap Post for one platform, or Post to all 4.
3. **Dry-run is on by default** — the dashboard checkbox is checked,
   and the CLI posts dry-run unless you pass `--live`. A dry run
   writes a receipt and makes zero network calls.
4. A live tap with a platform unconfigured fails cleanly
   ("not configured") and the clip stays approved — retry after setup.
5. Every attempt (dry-run / posted / failed / not_configured) is
   appended to that clip's `posts` list in `clips.json`, with a UTC
   timestamp, the privacy used, and the platform's id/URL when posted.

Privacy defaults (never public by default):
- YouTube: `private` — override with `YOUTUBE_PRIVACY=unlisted`
  (or `public`, only after Google's audit — see below).
- TikTok: `SELF_ONLY` — override with `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`
  only after TikTok's audit passes.
- Instagram / Facebook: posts to the account/Page as-is; there is no
  private-by-API option, so the approval tap *is* the safety check.

## 1. YouTube — Data API v3

**Where:** Google Cloud Console (console.cloud.google.com) → new
project → enable "YouTube Data API v3" → Credentials → Create OAuth
client ID (Desktop app).

**What you paste into `.env`:**
- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` — from that OAuth client.
- `YOUTUBE_REFRESH_TOKEN` — run `python3 posters/youtube_oauth.py`,
  open the URL it prints, sign in to your **personal** YouTube
  channel, paste the code back. It prints the refresh token; paste
  that into `.env`. The script writes nothing to disk.
- `YOUTUBE_PRIVACY=private` (default; leave it).

**Gate for public posting:** Google's compliance audit. Projects
created after 28 Jul 2020 have API uploads restricted to private
until the audit passes — there is no published timeline, and extra
quota (default is 100 uploads/day) is granted by application only,
not bought. Until it passes, your clips post as Private and you flip
them to Public in YouTube Studio by hand. A vertical video ≤ 3 min
is filed as a Short automatically; category is Gaming (20).

**Your step:** create the Cloud project + OAuth client, run the
oauth helper, paste 3 values into `.env`.

## 2. TikTok — Content Posting API (Direct Post)

**Where:** TikTok for Developers (developers.tiktok.com) → create an
app → add the **Content Posting API** product → turn on **Direct
Post** → add scopes `user.info.basic` (Login Kit) and `video.publish`.

**What you paste into `.env`:**
- `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` — from the app.
- `TIKTOK_ACCESS_TOKEN` — the user token for your creator account
  (authorise via Login Kit; tokens live 2 hours).
- `TIKTOK_REFRESH_TOKEN` — optional; refreshes the access token
  (180-day life). If set, the poster refreshes automatically.
- `TIKTOK_PRIVACY=SELF_ONLY` (default; leave it).

**Gate for public posting:** TikTok's Direct Post audit — a second
review after app review, with no published turnaround. Until it
passes, every Direct Post is forced to private (`SELF_ONLY`), only
from private accounts, max 5 users per 24h. The sandbox lets you test
the full flow first. Caps once live: ~15 posts per creator per day
(shared across apps). The poster queries creator info before every
Direct Post, as TikTok requires.

**Your step:** register the app, add the product + scope, authorise
your creator account, paste the tokens. Expect the audit to take a
while — dry-run and private posts work meanwhile.

## 3. Instagram — Meta Graph API (Reels)

**Where:** Meta for Developers (developers.facebook.com) → create a
**new, separate personal app** (not the Rivryn app) → add the
Instagram API with **Facebook Login** product.

**Prerequisites on your side:**
- Instagram account switched to Professional (Business or Creator).
  Personal accounts cannot publish via the API, at all.
- That account linked to a Facebook Page you admin.
- The Page's long-lived access token with permissions
  `instagram_basic`, `instagram_content_publish`,
  `pages_read_engagement` (request them in the app, grant them to
  the Page). The token goes in `.env` as `META_PAGE_ACCESS_TOKEN`.
- Your numeric Instagram user id → `INSTAGRAM_USER_ID`
  (find it in Meta's Graph API Explorer: `GET /me/accounts` then the
  linked `instagram_business_account` id, or the Business Settings page).

**What you paste into `.env`:**
- `INSTAGRAM_USER_ID`, `META_PAGE_ACCESS_TOKEN`
- `META_APP_ID` (optional, for reference)

**How it posts:** the poster creates a Reels container with
`upload_type=resumable`, uploads the file directly to Meta
(no public URL needed), polls until `FINISHED`, then publishes.
Graph API v26.0 is used.

**Gate for public posting:** none for your own accounts. Meta's
Standard Access covers apps that only serve accounts you own —
**no App Review is needed to post to your own Instagram**. App
Review + Business Verification only start if other people's accounts
connect to the app. (That is another reason to keep this app
separate from Rivryn's: Rivryn's app *will* need Advanced Access
for client accounts; this one never should.)

**Your step:** new personal Meta app, Professional IG account linked
to a Page, Page token with the three permissions, paste 2 values.

## 4. Facebook — Meta Graph API (Page Reels)

**Where:** the same separate personal Meta app as Instagram
(one app covers both — they share the Page token).

**Prerequisites:** a Facebook Page you admin (Reels publish to Pages
only — no API posts to personal profiles or Groups). Page token needs
`pages_manage_posts` (in addition to the Instagram permissions above).

**What you paste into `.env`:**
- `FACEBOOK_PAGE_ID` — numeric id of the Page.
- `FACEBOOK_PAGE_ACCESS_TOKEN` — if unset, it falls back to
  `META_PAGE_ACCESS_TOKEN` (same token usually works for both).

**How it posts:** `video_reels` three-phase flow — start (gets a
video id + upload URL), upload the bytes to `rupload.facebook.com`,
finish with `video_state=PUBLISHED` and the caption — then polls
until published. Limit: 30 API-published Reels per Page per 24h.
Clips are 30s, inside the 3–90s Reels window.

**Gate for public posting:** same as Instagram — no App Review for
your own Page (Standard Access). The Reel goes live on the Page
immediately when you tap Post, so the approval tap is the check.

**Your step:** confirm the Page + token have `pages_manage_posts`,
paste the Page id (token usually already in `.env` from Instagram).

## 5. Test it before trusting it

```bash
python3 post.py clip-02 --platform all            # dry-run (default)
python3 post.py clip-02 --platform youtube --live  # real post, YouTube only
```

Start with one platform, dry-run first, then one live post and check
the receipt in `clips.json` (external id + URL). The dashboard shows
the same receipts per clip, and the "Ready to post" filter lists
approved clips. A failed live post never marks anything posted —
fix the setup, tap Post again.

## What is still NOT here

- No scheduling, no auto-post, no background jobs. Every post is a
  tap. That is deliberate.
- No token refresh for Meta Page tokens (they are long-lived, ~60
  days; refresh them in Meta's token tool when one expires — the
  poster will fail cleanly with the platform's error).
- No public posting on YouTube/TikTok until their audits pass —
  the code cannot override that, and it never pretends otherwise.

## Docker (optional)

Docker packages the app so it runs the same everywhere — it does
NOT put the app on the internet by itself. For a public link you
still need the Cloudflare Tunnel (see TUNNEL.md / GO_LIVE.md) on
the mini, or you run this same setup on a VPS and point a DNS
record at it.

On the Mac mini (Docker Desktop is free for personal and small
business use):

```bash
cd hhsclip
docker compose up -d --build
```

Then open http://localhost:8000 (or http://<mini-LAN-IP>:8000 from
your phone on the same Wi-Fi). Accounts are made inside the
container and survive rebuilds:

```bash
docker compose exec app python3 accounts.py add <you> owner
docker compose exec app python3 accounts.py add shayne reviewer tech4health
```

Jobs, uploads, accounts, and downloaded transcription models all
live in Docker volumes, so `docker compose down` and rebuilding
the image do not wipe them. `docker compose down -v` DOES wipe
them — don't run that unless you mean it.

Note: the image has not been built yet — there is no Docker on
this build machine, so the first real `docker compose build` is
on the mini (or the VPS).
