# Secure Remote Access — Garmin Stats AI via Cloudflare Tunnel

How to reach the dashboard from anywhere at `https://kiwi.beanw.co.uk` without
exposing personal health data or the Anthropic API key to the public internet.

The app has **no built-in authentication** (see [README → Security](../README.md#security)).
We gate it at the Cloudflare edge with **Cloudflare Access (Zero Trust)** so only an
allow-listed identity ever reaches the Pi. No application code is changed.

> **This Pi already runs a Cloudflare tunnel** (it serves other apps such as Mealie).
> The tunnel is **dashboard / token-managed**, *not* `config.yml`-managed — so the
> setup below adds a **Public Hostname** to the existing tunnel in the Zero Trust
> dashboard rather than editing a local config file. If you are setting this up on a
> Pi with *no* tunnel yet, see [Appendix A](#appendix-a--first-tunnel-from-scratch).

---

## Architecture

```
Browser (anywhere)
   │  HTTPS to https://kiwi.beanw.co.uk
   ▼
Cloudflare Edge ── Cloudflare Access (Zero Trust) ──► allow-list: dan + helen emails
   │  (only authenticated sessions pass)
   │  existing outbound-only tunnel (no inbound ports on the Pi/router)
   ▼
cloudflared (systemd service, already running)
   │  proxies the kiwi.beanw.co.uk hostname to
   ▼
http://localhost:8081   ← garmin-insights FastAPI (Helen's env owns the web; START_WEB=true)
```

- **No inbound firewall ports / no port-forwarding** — `cloudflared` dials *out* to
  Cloudflare and holds the connection open.
- **Cloudflare Access** rejects unauthenticated requests at the edge, so the open
  FastAPI app is never reachable by an anonymous visitor.
- The Pi's real IP stays hidden behind Cloudflare.

---

## Current setup on this Pi (verified facts)

| Item | Value |
|---|---|
| `cloudflared` | installed at `/usr/local/bin/cloudflared`, run by `cloudflared.service` (systemd, enabled) |
| Tunnel type | **token / remotely-managed** (`cloudflared ... tunnel run --token …`) — ingress is configured in the dashboard, **there is no `~/.cloudflared/config.yml` in use** |
| Dashboard target | garmin web server at **`http://localhost:8081`** |
| Web owner | `users/helen.env` (`START_WEB=true`, `WEB_PORT=8081`). `users/dan.env` is fetcher-only (`START_WEB=false`) |
| Health endpoint | `GET /api/health?user=helen` (the bare `/api/health` returns 404 — it needs a real `?user=`) |
| AI provider | Anthropic (`claude-sonnet-4-6`) — `/api/chat` spends real Anthropic credits |
| Stale leftovers | `~/.cloudflared/cert.pem` + `~/.cloudflared/<UUID>.json` are from an old (2024) locally-managed tunnel and are **not used** by the running service. Leave them or delete them; they don't affect the token tunnel. |

---

## Setup runbook

### 1. Add the Public Hostname to the existing tunnel  *(Cloudflare dashboard)*

Cloudflare dashboard → **Zero Trust → Networks → Connectors** → open the **pi5**
tunnel → **Published application routes** tab → **Add** (in the older UI this was
**Public Hostname → Add a public hostname**):

- **Subdomain:** `kiwi`
- **Domain:** `beanw.co.uk` (the same zone your other tunnelled apps use)
- **Path:** *(leave blank)*
- **Service → Type:** `HTTP`
- **Service → URL:** `localhost:8081`

Save. Cloudflare creates the `kiwi.beanw.co.uk` DNS record (proxied) automatically.

> This replaces the plan's `cloudflared tunnel create` + `config.yml` +
> `cloudflared tunnel route dns` steps — those only apply to a locally-managed tunnel.
> Because this tunnel is token-managed, everything is done in the dashboard.

### 2. Lock the front door with Cloudflare Access  *(the critical step)*

Cloudflare dashboard → **Zero Trust → Access → Applications → Add an application →
Self-hosted**:

- **Application name:** `Garmin Stats`
- **Session duration:** e.g. `24h` (or `1 week` for less re-auth; convenience vs. risk).
- **Public hostname:** `kiwi` . `beanw.co.uk` (matches step 1).
- **Identity / login method:** **Google SSO** is set up as the login method (see
  [Appendix C](#appendix-c--google-sso-login-method)). One-time PIN (email OTP) is the
  zero-setup fallback.
- **Policies:**
  - Policy 1 — **Action: Allow**, rule **Emails** → add Dan's + Helen's Google emails only.
  - Policy 2 — **Action: Block**, rule **Everyone** (default deny — optional, since an
    Allow-only policy already denies everyone else by default).
- Save.

### 3. (Recommended) Rate-limit `/api/chat` at the edge  *(Cloudflare dashboard)*

Each `/api/chat` call can run up to 10 Claude tool-calling rounds and spends real
Anthropic credits. There is **no app-level rate limit**. Add an edge rule so even an
authenticated session can't burn the budget:

- **Security → WAF → Rate limiting rules → Create rule** on `kiwi.beanw.co.uk`:
  match path `/api/chat`, e.g. **> 20 requests / minute per IP → Block (or Managed Challenge)**.

Independently, in the **Anthropic console** set a **monthly budget cap** on the API
key so a runaway loop or leaked key can't drain the account.

### 4. Verify

```bash
# On the Pi — service healthy and tunnel registered
systemctl status cloudflared --no-pager
journalctl -u cloudflared -n 30 --no-pager        # look for "Registered tunnel connection"

# Local app reachable (note the ?user= — bare /api/health 404s)
curl -sf "http://localhost:8081/api/health?user=helen" && echo OK
```

Then, from a browser:

1. **Anon blocked (most important test):** open `https://kiwi.beanw.co.uk` in a
   logged-out / incognito window → you should see the **Cloudflare Access login**, not
   the dashboard.
2. **Allowed users pass:** log in as Dan and as Helen → dashboard loads, the user
   picker switches between both.
3. **Disallowed identity blocked:** log in with a non-listed email → access denied.
4. **Rate-limit sanity:** rapid repeated hits to `/api/chat` trip the WAF rule
   (check Zero Trust / WAF analytics).

---

## Data-safety measures (independent of the access gate)

1. **Secrets hygiene — already in place.** `users/*.env`, root `.env`, and `*.db` are
   git-ignored (`users/.gitignore` keeps `*.env`, allows `*.env.example`; root
   `.gitignore` ignores `.env`, `*.db`, `garmin.db`). Confirm nothing leaked:
   ```bash
   git -C /home/dan/garmin-data ls-files | grep -iE '\.env$|\.db$'   # expect no real files
   git -C /home/dan/garmin-data grep -i 'sk-ant' -- ':!*.example'    # expect nothing
   ```
2. **Tighten file permissions (optional, recommended).** The SQLite DBs and env files
   are currently world-readable (`644`). Restricting to the owner closes local-user
   snooping:
   ```bash
   chmod 600 /home/dan/garmin-data/*.db /home/dan/garmin-data/users/*.env /home/dan/garmin-data/.env
   ```
   (Safe: the fetcher + web both run as `dan`. Skip if another local service needs to
   read these files.)
3. **No secret leakage via the API — confirmed.** `/api/health` returns the model
   *name* only, never the key. No endpoint dumps config.
4. **Backups.** Each user's `*.db` is the single source of truth and is not in git.
   Add a periodic encrypted off-box backup so an SD-card failure doesn't lose history:
   ```bash
   # nightly example: consistent snapshot → encrypt → copy off-box
   sqlite3 /home/dan/garmin-data/helen.db ".backup '/tmp/helen.db.bak'"
   age -r <your-age-recipient> -o /tmp/helen.db.bak.age /tmp/helen.db.bak && rm /tmp/helen.db.bak
   # then rsync/scp /tmp/helen.db.bak.age off the Pi
   ```
5. **Keep cloudflared + OS patched.** The service is currently a few versions behind
   (it warns in `journalctl`). `apt upgrade` on a schedule; re-download the
   `cloudflared` ARM64 binary and restart the service when it lags.

---

## Residual risks of "edge gate only" (accepted)

The user opted for an edge gate with **no app code changes**. Honest residual risks:

| Residual risk | Why it remains | Optional low-effort mitigation |
|---|---|---|
| **App answers on the whole LAN** (`0.0.0.0:8081`) — anyone on the home WiFi (guest, IoT device) reaches the full dashboard with no login | Edge gate protects the *internet* path, not the LAN | **Kept `0.0.0.0` by user choice** (home devices use the LAN IP today). To close it later: set `WEB_HOST=127.0.0.1` in `users/helen.env` and restart Helen's web — the tunnel still reaches `localhost`, so only remote access via `kiwi.beanw.co.uk` survives. |
| **AI chat cost abuse** — an authenticated (or LAN) user can spam `/api/chat` and burn Anthropic credits | No app-level rate limit | Cloudflare WAF rate-limit rule (step 3) + Anthropic monthly budget cap |
| **No per-user privacy** — once through the gate, the picker shows both Dan's and Helen's data | App has no identity awareness | Accepted ("shared is fine") |
| **No app audit log** of who viewed what | App logs nothing | Cloudflare Access logs every authenticated request (who / when) in the Zero Trust dashboard |

---

## Appendix A — first tunnel from scratch

Only needed on a Pi with **no** existing tunnel. (This Pi already has one — use the
dashboard steps above instead.)

```bash
# 1. Install (ARM64 / Pi 4/5 64-bit — check: uname -m → aarch64)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
cloudflared --version

# 2. Create the tunnel from the dashboard (Zero Trust → Networks → Tunnels →
#    Create a tunnel → Cloudflared) and copy the install command it shows —
#    it installs the service with the connector token (the modern, dashboard-managed
#    approach, identical to how this Pi's tunnel already runs).

# 3. Add a Public Hostname and Access policy exactly as in steps 1–2 above.
```

The older CLI-managed flow (`cloudflared tunnel login` → `tunnel create <name>` →
`~/.cloudflared/config.yml` ingress → `tunnel route dns`) still works but produces a
locally-managed tunnel whose ingress lives in a file. This Pi does **not** use that
model; prefer the dashboard token approach for consistency.

---

## Appendix B — optional future path: Tailscale (private, not built now)

For a private path with no public DNS and no Cloudflare in the data path:

- `curl -fsSL https://tailscale.com/install.sh | sh` on the Pi and each client; `sudo tailscale up`.
- Reach the dashboard at `http://<pi-tailscale-name>:8081` over the tailnet.
- Optionally **Tailscale Serve/Funnel** for HTTPS or **MagicDNS** for a friendly name;
  ACLs restrict which tailnet members reach the Pi.
- Runs **alongside** Cloudflare Access (Cloudflare = convenient public door, Tailscale =
  private fallback) — they don't conflict.

---

## Appendix C — Google SSO login method

Used as the login method for the Access app in step 2 so Dan and Helen sign in with
their Google accounts (one click, no email OTP codes). Requires a Google Cloud OAuth
client. One-time done; thereafter it's a dropdown choice on any Access app.

**One-time prerequisite — note your Access team domain.** Zero Trust →
**Settings → Custom Pages** (or **Team domain**) shows it as
`<team>.cloudflareaccess.com`. You need it for the redirect URI below.

### C.1 Create the Google OAuth client  *(console.cloud.google.com)*

1. Create or pick a project.
2. **APIs & Services → OAuth consent screen** → User type **External** → fill app name +
   support email → add Dan's and Helen's Google emails as **Test users** (or **Publish**
   the app so any Google account can authenticate — the *Access* email policy still
   restricts who actually gets in).
3. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   Application type **Web application**.
4. **Authorized redirect URI:**
   `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`
5. Create → copy the **Client ID** and **Client secret**.

### C.2 Add Google as a login method in Cloudflare

Zero Trust → **Settings → Authentication → Login methods → Add new → Google**
(in the redesigned UI this lives under **Team & Resources → Authentication** or
**Access controls**). Paste the **Client ID** + **Client secret**, save, then click
**Test** to confirm the round-trip works.

### C.3 Attach it to the app

In the Access app (step 2) login-method screen, enable **Google** (and optionally
disable One-time PIN if you want Google-only). The email Allow policy is what actually
gates access — Google SSO just authenticates identity.
