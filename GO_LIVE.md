# HHS Clips go-live plan — mini + Cloudflare Tunnel

**Goal:** the app live at `https://clips.helpinghandssystems.com`, Shayne
signing in to review Tech4Health's clips. Cost: $0 (Cloudflare Tunnel
is free; the mini does the work). Full tunnel steps live in `TUNNEL.md` —
this is the order to do everything in, and who does what.

## Order

### 0. Ship the code first (Turdy, then Brendon's merge tap)
- PR #6: client sign-in + Classic Dew colors + space background.
- Brendon merges. **Nothing below works until this is merged** — the
  sign-in the public link depends on is in this PR.

### 1. On the mini: get the latest code
```bash
cd hhsclip
git pull   # or re-clone if the mini has no checkout yet
```

### 2. Turn sign-in on (on the mini, before any public link)
```bash
python3 accounts.py add <your-name> owner
python3 accounts.py add shayne reviewer tech4health
```
Set a password for each when prompted. No accounts = no login = anyone
with the link could use the app.

### 3. Start the app (on the mini)
```bash
python3 server.py --host 0.0.0.0
```
Check on the mini itself: `http://localhost:8000` shows the sign-in
page. Sign in as the owner and check a job loads.

### 4. Quick tunnel test — temporary link, no account needed
```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```
Open the `https://something-trycloudflare.com` link it prints, on your
phone. You should see the sign-in page. **This link changes every
restart** — first check only, not the link for Shayne.

### 5. Stable link — DECISION NEEDED (Cloudflare Free can't do partial DNS)
CORRECTION 2026-11-20: Cloudflare's partial (CNAME) setup — the
"keep DNS on Netlify" path — is Business-plan only ($200/mo), not
Free. The earlier partial-setup steps below are WRONG for a Free
account; do not follow them. The apex zone
`helpinghandssystems.com` already exists in Cloudflare as a
PENDING full zone (nameservers NOT changed; DNS still on Netlify —
harmless, remove it if another path is chosen). Paths:
(a) Subdomain zone — add `clips.helpinghandssystems.com` as its own
Cloudflare zone and delegate just that subdomain from Netlify with
NS records. Free IF Cloudflare allows it on Free (unverified —
test by adding the zone; some sources say subdomain setups are
Business/Enterprise too). Apex DNS stays on Netlify.
(b) Full setup — move `helpinghandssystems.com` nameservers to
Cloudflare (free). Verify every imported record against Netlify's
zone first (the import showed 6 A, 4 AAAA, 1 MX, 6 TXT — no CNAMEs
listed; a missed record breaks a site).
(c) Fly.io ~$8/mo — one plain CNAME in Netlify, no Cloudflare.
Whichever path: prove it free first with the quick tunnel
(step 4) before any money or DNS change.
Open `https://clips.helpinghandssystems.com` on your phone — the lock
icon is automatic. Leave that Terminal window running.

### 6. Verify live (from your phone)
- Sign in as the owner at the stable link; a job loads.
- Tag a Tech4Health job to client `tech4health` (client box on the
  job card, "Tag client").

### 7. Send Shayne in
- The link: `https://clips.helpinghandssystems.com`
- Her username + password — **in a separate message** from the link.
- She signs in, lands on her review page, sees only Tech4Health's
  clips, taps Approve or Ask for changes. Her taps land straight in
  the app; nothing posts anywhere.

## Keep it alive
- The mini must stay **on and awake** while the app should answer
  (System Settings → Energy Saver: display may sleep, computer must
  not — or run `caffeinate -i` in a Terminal window).
- `cloudflared tunnel run hhsclip` must keep running. If it stops,
  the link stops. Autostart on boot (launchd) is a later step — ask
  if you want it.
- If the mini is off, there is no fallback. A hosted box (Fly.io,
  ~$8/mo) is the alternative if that becomes a problem.

## If it doesn't answer
See "If it doesn't answer" in `TUNNEL.md`: check the server is
running, the mini is awake, and the tunnel is running — in that order.
