# hhsclip — HHS Clips (standalone app)

**What this is, in plain language:** you drop in a long video you own —
a podcast, talk, webinar, testimonial, demo, sermon, or a game stream
VOD — and the app cuts the best short vertical moments out of it with
captions. You approve or reject each clip on your phone, then post the
approved ones to YouTube, TikTok, Instagram and Facebook — only when
you tap Post. Nothing ever posts on its own.

**Own videos only.** Every job needs an ownership attestation ("I own
this video or have rights to post clips from it") before it runs — on
the command line (`--i-own-it`) or the checkbox in the app. This app
is not a tool for clipping other people's streams.

Free and open source (MIT). Everything runs locally on your own
machine — no cloud API keys, no per-minute bill, no footage leaving
your computer.

## How it works

1. **Ingest** — upload a video file in the app, paste a video link
   (YouTube / Twitch / Kick VOD via `yt-dlp`), or pass a local file /
   `--url` to the CLI.
2. **Transcribe locally** — `faster-whisper` (tiny model by default,
   CPU-friendly) with word timestamps. No audio is sent anywhere.
3. **Score, in two modes** (`--mode gameplay|talking|auto`):
   - **gameplay** — audio-energy / loudness spikes, excited speech
     (keywords, exclamations, laughter), speech-density changes.
     For game streams.
   - **talking** — transcript-based: strong opening lines (hooks),
     short quotable lines, questions, story turns, emotional words,
     and whether the window starts/ends on a sentence boundary (a
     clean cut). For podcasts, talks, webinars, testimonials.
   - **auto** (default) — talking when speech is dense (≥ 1 word/sec
     on average), gameplay when it isn't or there is no transcript.
     Explicit `--mode talking` with no transcript falls back to
     gameplay scoring and says so in the manifest.
   Top 5 windows win, with a gap rule so you don't get the same
   moment twice.
4. **Cut** — `ffmpeg` reframes to 9:16 vertical (720×1280, center
   crop), burns in word-level captions from the transcript, outputs
   MP4 + SRT per clip.
5. **Approve** — the app shows each clip with video preview, score
   and reason, and Approve / Reject / Edit-title buttons. Status
   saves back to `clips.json`.
6. **Post** — approved clips only, per platform or all four, dry-run
   by default (see "Posting" below).

## Setup

Requires: Python 3.10+, `ffmpeg` + `ffprobe` on PATH.

```bash
cd hhs-clips
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

No API keys. No accounts. The first transcription downloads the
Whisper model (~75 MB for `tiny`) into the local Hugging Face cache.

## Run the app

```bash
python3 server.py
# open http://localhost:8000 on this machine,
# or run with --host 0.0.0.0 and open http://<this-machine's-LAN-IP>:8000
# on your phone (same Wi-Fi). Single-tenant local app — no accounts in v1.
```

- **＋ New** — upload a file or paste a link, pick a mode and model,
  tick the ownership box, tap Cut clips. The job runs in the
  background; the page shows progress and a log tail, then links to
  the queue when it is done.
- **Jobs** — every job with source, mode, clip count, approve/reject
  counts and status; open any finished job's queue.
- **Queue** — approve / reject / edit title, then Post per platform
  or Post to all 4 (dry-run is on by default).

The app is phone-first (390px): big Approve / Reject buttons, video
preview per clip, editable title/description.

## Run the clipper from the command line

```bash
# From a local file you own
./venv/bin/python clipper.py input/your-video.mp4 --i-own-it

# From a video link instead
./venv/bin/python clipper.py --url "https://www.youtube.com/watch?v=..." --i-own-it

# Options
./venv/bin/python clipper.py input/video.mp4 --i-own-it --mode talking \
  --model small   # tiny | small | base — tiny is fastest on CPU, small/base are more accurate
# --mode: gameplay | talking | auto (default auto)
# --num-clips 5 --clip-seconds 30
```

Without `--i-own-it` the job is refused before anything is
downloaded or cut. Output lands in `output/job-YYYYMMDD-HHMMSS/`:
- `clip-01.mp4` … `clip-05.mp4` — 720×1280 vertical clips
- `clip-01.srt` … — caption files (only when transcription produced words)
- `clips.json` — the manifest: mode, ownership attestation
  (`owned: true`, `attested_at` UTC), scores, reasons, transcript
  excerpts, draft titles/descriptions/hashtags, and each clip's
  approval status (`pending` to start)

## Degraded mode (honest fallback)

If `faster-whisper` can't be installed or transcription produces no
words, the clipper **does not fake captions**. It falls back to
audio-energy-only scoring (`--no-whisper` forces this), skips the
burned-in captions, leaves `srt: null` in the manifest, and records
`"scoring": "audio energy only (no transcript)"` in `clips.json`.
Talking mode with no transcript falls back to gameplay scoring and
records `mode_fallback` in the manifest. You still get real clips,
just without speech-based scoring or captions.

Known quirk (found during testing, 2026-11-18): faster-whisper 1.2.1
decodes file paths through PyAV, but PyAV 19 removed the
`metadata_errors` keyword faster-whisper passes, so transcription
crashes on a file path. The clipper works around it by decoding the
audio to PCM with `ffmpeg` first and passing the samples to Whisper
as a numpy array, bypassing PyAV entirely. If you pin `av<15`
instead, the file-path route works too — the PCM route is kept
because it works with any PyAV.

## Posting (built 2026-11-18) — approval-gated

`posters/` holds one adapter per platform (stdlib `urllib` only —
`requests` was not needed), plus `posters/gate.py`, the single gate
every post goes through:

- **Only approved clips post, and only from attested jobs.** A job
  without an ownership attestation cannot post at all, even if a clip
  in it is approved. Pending/rejected clips are refused (HTTP 403
  from the server, exit code 2 from the CLI) before any network call.
- **Dry-run by default.** The CLI posts dry-run unless you pass
  `--live`; the dashboard's dry-run checkbox starts checked. A dry
  run writes a receipt and makes zero network calls.
- **Receipts.** Every attempt (dry-run / posted / failed /
  not_configured) is appended to that clip's `posts` list in
  `clips.json` with platform, privacy, UTC timestamp, external id/URL
  when posted, and the error text when it fails. A failed post never
  marks the clip posted; it stays approved and retryable.
- **Safe privacy defaults.** YouTube `private`, TikTok `SELF_ONLY`
  — until Google's / TikTok's audits pass, public posting is not
  possible anyway, and the code never claims otherwise.
- **Credentials are env vars only** (`.env`, git-ignored; see
  `.env.example` for the exact names). No credentials, no posting:
  the endpoint fails cleanly with "not configured" and dry-run
  still works.

```bash
cp .env.example .env   # fill in your values — see SETUP.md per platform
python3 post.py clip-02 --platform all            # dry-run (default)
python3 post.py clip-02 --platform youtube --live  # real post, YouTube only
```

In the app, approved clips show Post buttons per platform + Post to
all 4, a "Ready to post" filter, and their receipts.
`GET /api/posting/config` reports which platforms are configured
(booleans only — never a secret). Full per-platform setup is in
[SETUP.md](SETUP.md).

Tests: `python3 -m unittest discover -s tests -v` — 24 tests, mocked
HTTP only (gate refusals, dry-run makes no network calls, receipt
shape, privacy defaults, not-configured handling, a mocked YouTube
resumable-upload pass, mode selection, talking-head scoring, and the
ownership gate on the CLI, the jobs API, and the posting gate). No
test touches a real platform.

## What is NOT in v1

- **No accounts / multi-user.** Single-tenant local app. Don't expose
  it to the public internet; bind to localhost, or your own LAN.
- **No hosted processing.** Video work runs on the machine the app
  runs on (your Mac mini / local machine).
- **No Rivryn link.** No sharing clips into Rivryn's social planner
  yet — later slice, only if asked.
- **No auto-post, no scheduling.** Every post is a tap on an approved
  clip. There is no background job, no timer, no "post all new clips"
  path.
- No cloud services, no database (JSON files only).
- No face-tracking reframe — v1 is a center crop. No auto-zoom.
- No AGPL code was copied. Clips Kitty (AGPL) was studied as a
  reference only; this code is original, MIT-licensed.

## Repo notes

- `input/`, `output/`, `venv/`, and model caches are git-ignored — never commit video or keys.
- Never put API keys in code. Slice 2 keys go in environment variables / a `.env` file (also ignored).
- `LAUNCH_DAY.md` was written for the GTA 6 launch and is stale now
  that this is a general-video app — it needs a refresh or removal
  before the repo goes public.
