# cloudflared — Garmin Stats remote access

Full runbook: [`../../docs/deployment-security.md`](../../docs/deployment-security.md).

## How this Pi's tunnel works (important)

This Pi's `cloudflared` is **dashboard / token-managed**, not `config.yml`-managed.
The service runs as:

```
cloudflared --no-autoupdate tunnel run --token <CONNECTOR_TOKEN>
```

so **ingress (public hostnames) is configured in the Cloudflare Zero Trust dashboard**,
not in a local file. There is intentionally **no `config.example.yml` in this folder** —
it would not reflect reality and could mislead. To expose the garmin dashboard you add a
**Public Hostname** to the existing tunnel and an **Access** policy, both in the dashboard.
See the runbook.

> A `config.example.yml` *would* be the right artifact only for a locally-managed tunnel
> (the `cloudflared tunnel create` + `~/.cloudflared/config.yml` flow). This Pi does not
> use that model — see Appendix A of the runbook if you ever set one up from scratch.

## Quick reference

- **Public hostname:** `kiwi.beanw.co.uk` → `http://localhost:8081`
- **Web owner:** `users/helen.env` (`START_WEB=true`, `WEB_PORT=8081`)
- **Access policy:** Allow = Dan + Helen emails; default Block everyone else
- **Health check (local):** `curl -sf "http://localhost:8081/api/health?user=helen"`

## Service commands

```bash
systemctl status cloudflared --no-pager
journalctl -u cloudflared -f          # watch tunnel connections / origin errors
sudo systemctl restart cloudflared    # after a binary upgrade
```

## Keep it patched

`cloudflared` runs with `--no-autoupdate`. When `journalctl` warns the version is
outdated, re-download the ARM64 binary and restart:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
sudo systemctl restart cloudflared
cloudflared --version
```
