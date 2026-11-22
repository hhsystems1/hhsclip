# HHS Clips on a VPS — for Brendon (and his brother)

**In plain language:** a VPS is a small computer you rent in a
datacenter — like the Mac mini, but it lives on the internet with
its own public address and stays on all the time. Docker is what
runs the app on it: the same files that run on the mini run here,
unchanged. This path needs **no Cloudflare and no tunnel** — your
Netlify DNS just points at the VPS directly.

**Cost:** roughly $4–6 a month for a small server (Hetzner Cloud
or DigitalOcean both have one at that size; prices move, check
before you buy). Split it however you like — it's one box you can
both log in to.

**Step 0 — PR #6 must be merged first.** The Docker files
(Dockerfile, docker-compose.yml, Caddyfile) land in the repo with
that PR. Until then they're only in staging here.

## 1. Create the server (either of you, ~10 minutes)

- Provider: **Hetzner Cloud** — server type **CX22** (renamed CX23
  in 2026, same machine), about **€4.50 a month**: 2 vCPU, 4 GB
  RAM, 40 GB disk, 20 TB traffic included. Region: a US one
  (Ashburn or Hillsboro). The easier alternative is DigitalOcean
  ($12/mo for 2 GB / 1 vCPU, $18 for 2 vCPU) — friendlier
  dashboard, costs more for less machine. The steps below are the
  same either way.
- Image: Ubuntu 24.04. Size: the smallest
  (1 vCPU, 2 GB RAM is enough to start; transcription is CPU work,
  so a bigger box just cuts faster).
- Access: add your SSH key when it asks (both of you can add a
  key, so you each have your own login). If you'd rather use a
  password, that's fine too — pick a strong one.
- Write down the server's public IP address. You need it in step 3.

## 2. Install Docker and start the app (on the server)

Log in over SSH, then:

```bash
# Docker (one command, from Docker's own install docs)
curl -fsSL https://get.docker.com | sh

# The app (public repo, no login needed)
git clone https://github.com/hhsystems1/hhsclip.git
cd hhsclip

# Build + start, with automatic HTTPS (Caddy profile)
docker compose --profile vps up -d --build
```

Check it answers locally first:

```bash
curl -s http://localhost:8000/ | head -5
```

## 3. Turn sign-in on BEFORE the public link goes live

Same as on the mini — no accounts means anyone with the address
could use the app:

```bash
docker compose exec app python3 accounts.py add <your-name> owner
docker compose exec app python3 accounts.py add shayne reviewer tech4health
```

## 4. Point Netlify DNS at the server

In Netlify DNS for `helpinghandssystems.com`, add one record:

- Type: **A**, Name: `clips`, Value: the server's public IP from
  step 1.

That's the whole DNS change — one A record, no Cloudflare, no
nameserver move. (Do NOT point a CNAME at any
`*.cfargotunnel.com` address — that only works inside Cloudflare.)

Wait a few minutes for DNS to spread, then open
`https://clips.helpinghandssystems.com`. Caddy fetches the
certificate automatically, so the lock icon is there with nothing
else to do.

## 5. Send Shayne in

- The link: `https://clips.helpinghandssystems.com`
- Her username + password — **in a separate message** from the link.
- She signs in, lands on her review page, sees only
  Tech4Health's clips.

## Keep it safe

- In the provider's firewall, allow only **22 (SSH), 80, 443**.
  Port 8000 is only for Caddy inside the machine — nothing on the
  internet should reach it directly.
- Updates: `cd hhsclip && git pull && docker compose --profile vps up -d --build`.
  Jobs, uploads, and accounts live in Docker volumes, so a rebuild
  does not wipe them. `docker compose down -v` DOES wipe them —
  don't run that unless you mean it.
- Backups: the volumes hold everything (`hhsclip-output`,
  `hhsclip-state`, …). If the box ever dies, those are what you'd
  want a copy of — ask if you want a backup step added.

## If it doesn't answer

- **Page never loads:** is the container running?
  `docker compose ps` should show `app` and `caddy` both up.
  Is DNS pointing at the right IP? (`nslookup
  clips.helpinghandssystems.com` should show the server's IP.)
- **Certificate error:** Caddy needs port 80 open to fetch the
  certificate — check the firewall allows 80, then
  `docker compose logs caddy`.
- **Sign-in page shows, but login fails:** the account doesn't
  exist yet — run `docker compose exec app python3 accounts.py list`.
