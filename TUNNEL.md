# HHS Clips on the web — free Cloudflare Tunnel from the Mac mini

**In plain language:** the app already runs on the mini. A Cloudflare
Tunnel gives it a public web address without opening any ports on
your network — the mini reaches *out* to Cloudflare, and Cloudflare
forwards visitors in. Same code, same dashboard, just at a web
address instead of `localhost`. The local part keeps working exactly
as it does now.

> **Do this FIRST: turn sign-in on before any public link goes live.**
> Run `python3 accounts.py add <you> owner` and
> `python3 accounts.py add <reviewer> reviewer <client>` (see
> "Client sign-in" in README.md). Right now the app has no login at
> all — a public link without sign-in means anyone finding the
> address could upload videos and burn your machine.

> **The tunnel itself is unverified on the mini.** The commands below
> were checked against Cloudflare's own docs (updated 2026-10-13 and
> 2026-09-30), but nobody has run them on Brendon's Mac mini yet —
> the first real run there is the proof.

## 0. What you need

- The app running on the mini: `python3 server.py --host 0.0.0.0`
  (so it listens beyond localhost — the tunnel connects to it at
  `http://localhost:8000`).
- The mini **on and awake** whenever the web app should answer. In
  System Settings → Energy Saver, set the display to sleep but
  prevent the computer sleeping automatically — or, while serving,
  run `caffeinate -i` in a Terminal window to keep it awake.
- A free Cloudflare account for the stable link (step 2). The quick
  test in step 1 needs no account and no domain.

## 1. Quick test — a temporary link, no account, no domain

On the mini:

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

That prints a link like `https://something-trycloudflare.com`. Open
it on your phone — you should see the HHS Clips sign-in page. Sign
in as the owner and check a job loads. **The link changes every time
you restart the tunnel** — it is for a first check only, not the link
you send a client.

## 2. Stable link — see GO_LIVE.md §5 for the current options

CORRECTION 2026-11-20: Cloudflare's partial (CNAME) setup is
Business-plan only ($200/mo) — NOT available on Free. The
partial-setup steps that were here are wrong for a Free account
and have been removed. The apex zone `helpinghandssystems.com`
exists in Cloudflare as a PENDING full zone (nameservers NOT
changed; DNS still on Netlify). See GO_LIVE.md §5 for the three
live options: subdomain zone, full nameserver move, or Fly.io.

Do **not** CNAME straight to `<tunnel-id>.cfargotunnel.com` from
Netlify — that name only resolves inside Cloudflare's proxy, so it
would point at nothing. `cloudflared tunnel route dns` only works
when the zone is fully on Cloudflare.

On the mini:

```bash
# Sign in to Cloudflare in the browser (picks your account + domain)
cloudflared tunnel login

# Make the tunnel and give it a name
cloudflared tunnel create hhsclip
# Prints a tunnel ID (a long UUID) and saves credentials to
# ~/.cloudflared/<TUNNEL-ID>.json — keep that file, it is the key.

# Point a hostname at the tunnel (creates the DNS record for you)
cloudflared tunnel route dns hhsclip clips.helpinghandssystems.com
```

Then create `~/.cloudflared/config.yml` (replace `<TUNNEL-ID>` with
the ID from the step above):

```yaml
tunnel: <TUNNEL-ID>
credentials-file: /Users/<you>/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: clips.helpinghandssystems.com
    service: http://localhost:8000
  - service: http_status:404
```

Run it:

```bash
cloudflared tunnel run hhsclip
```

Open `https://clips.helpinghandssystems.com` — Cloudflare handles
the certificate, so the lock icon is there automatically. Leave that
Terminal window running (or set it up to start on boot — that's a
later step, ask if you want it).

## 3. What to send the reviewer

- The link: `https://clips.helpinghandssystems.com` (or the
  temporary trycloudflare link for a first test).
- Their username and password — **in a separate message** from the
  link, or in person. Usernames are made with
  `python3 accounts.py add <name> reviewer <client>`; passwords are
  reset the same way (`python3 accounts.py passwd <name>`).

When Shayne signs in she lands on her own review page — only
Tech4Health's clips, Approve / Ask for changes, nothing else.

## If it doesn't answer

- **The page never loads:** is `server.py` running on the mini, and
  is the mini awake? Is `cloudflared tunnel run hhsclip` still
  running in its Terminal?
- **Cloudflare shows an error page:** the tunnel is up but the app
  isn't answering on `http://localhost:8000` — check the server
  window for a crash or a port already in use.
- **Sign-in page shows, but login fails:** the username or password
  is wrong, or the account doesn't exist yet — run
  `python3 accounts.py list` on the mini to check.

## Costs and limits

- Cloudflare Tunnel is free (unlimited tunnels and bandwidth on the
  free tier). No port forwarding, no firewall change.
- The mini does all the work — transcription and cutting run on it,
  exactly as they do locally. If the mini sleeps, the web app sleeps
  with it.
- The tunnel is one machine's outbound connection: if the mini is
  off, there is no fallback. A hosted box (Fly.io, ~$8/mo) is the
  alternative if the mini being off becomes a problem.
