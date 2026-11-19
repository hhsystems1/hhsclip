# Clips app for HHS — v1 built 2026-11-18 (standalone)

What it is, in plain language: a business drops in a long video they own
— a podcast, webinar, testimonial, demo, sermon, or a stream VOD — and
the app cuts the best short vertical moments, they approve them on their
phone, and it posts the approved ones to their connected YouTube,
Facebook, Instagram, TikTok (X later).

Who it's for: HHS itself first, then small businesses using it as a
standalone app. Single-tenant local app in v1 — one user, their own
videos, their own connected accounts.

## What already exists (reuse, don't rebuild)

- Slices 1+2 local engine: ingest file/URL, local transcription, gameplay
  scoring, 9:16 cut + captions, approval queue, approval-gated posting
  adapters with dry-run + receipts.
- Rivryn social stack: social_accounts/posts/messages/metrics, composer +
  scheduler, Meta publish (FB video, IG Reel), owner/admin-gated
  publishing, per-workspace Connections, approvals center. Nothing
  publishes without approval — already the house rule.

## What changes from the personal clipper

1. General video, not just game streams: two scoring modes — gameplay
   (audio spikes + excited speech, built) and talking-head (transcript
   virality: hooks, quotable lines, story peaks — built in v1).
2. Own content only: the app accepts video the workspace owns or has
   rights to. That settles the permission question by design — no
   clipping other people's streams.
3. Multi-workspace: NOT in v1 — v1 is single-tenant local (one user,
   their own videos, their own accounts). Org-scoped clips are a
   later slice.
4. Runs as an app, not a CLI on one Mac: processing location is open —
   Rivryn runner/harness job vs a local clipper that syncs. Needs a
   decision before building the hosted path.

## Decisions

- Placement DECIDED 2026-11-18: standalone HHS clips app (not inside
  Rivryn). Own repo, own codebase, built on slices 1+2.
- Name DECIDED 2026-11-18: `hhsclip` (repo `hhsystems1/hhsclip` once
  Brendon creates it).
- Pricing/tier: fits the runs-metering model (each clip job = runs);
  prices not decided.
- First users: HHS itself first, then client workspaces?

## v1 BUILT 2026-11-18 (verified, see README)

- Talking-head scoring mode (`--mode gameplay|talking|auto`) alongside
  the unchanged gameplay mode; `mode` recorded in clips.json.
- Own-content gate (the permission fix, by design): no job starts
  without an ownership attestation — `--i-own-it` on the CLI, a
  checkbox in the app; `owned: true` + `attested_at` stored in the
  job manifest, and the posting gate refuses unattested jobs.
- Standalone app shell: upload in the UI (file or link), jobs list,
  approval queue + approval-gated posting, all stdlib server.py,
  phone-first at 390px.
- Single-tenant local app: no auth/multi-user in v1 (README says so).

## Later (only if asked)

- Accounts/multi-user, hosted processing, Rivryn link (share clips
  into Rivryn's social planner).
