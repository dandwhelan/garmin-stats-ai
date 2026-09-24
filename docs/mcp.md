# Garmin Insights MCP server

`garmin-insights-mcp` exposes one user's Garmin database to any MCP client
(Cursor, Claude Desktop, Antigravity) — the same query tools, scan prompts and
science guardrails the in-app agent uses. It never calls Anthropic itself: the
client's own model does the reasoning, the server supplies data and context.

- **One server = one user.** There is no user switching inside a server.
- **Read-only by default.** `--allow-writes` adds `save_user_note`,
  `save_daily_note`, `start_experiment` and `evaluate_experiment`.
- Independent of the web dashboard — it reads the SQLite DB directly.

## Which server is which

Both run always-on on the Pi as instances of one systemd template unit:

| Client entry | URL | systemd unit | Data |
|---|---|---|---|
| `garmin-dan` | `http://192.168.4.148:8765/mcp` | `garmin-insights-mcp@dan` | `users/dan.env` → Dan's DB |
| `garmin-helen` | `http://192.168.4.148:8766/mcp` | `garmin-insights-mcp@helen` | `users/helen.env` → Helen's DB |

The unit passes `--user %i`; the port comes from `deploy/mcp-<user>.env`.

Each server also **identifies itself**, so a swapped port can't silently mix
people up: the MCP server name is `garmin-insights-<user>`, its instructions
open with *"Every tool on this server reads <Name>'s database only… if the
conversation is about someone else, you are on the wrong server"*, and
`garmin://profile` / `get_current_context` both report the user.

## What a server exposes

### Instructions (sent once at connect)

The in-app agent's full static system prompt, built by calling the agent's own
methods so the two cannot drift apart:

- which user's data this is (name, user id, biological sex)
- base analysis rules (sleep keyed to wake-up date, today's cumulative metrics
  incomplete, fetch minimal ranges, experiment protocol)
- the medical knowledge base — every evidence-graded rule, filtered by sex
  (cycle rules are hidden from male users)
- the evidence-tier output rules and mandatory wording substitutions
  ("illness-like recovery strain pattern", never "diagnose"; "physiological
  strain", not "mental stress"; SpO2 → "screening signal", never "sleep apnoea")
- the identity block (male users are told they have no cycle data)

### Tools

| Tool | Purpose |
|---|---|
| `get_current_context` | **Call first.** Whose data this is, today's date, current menstrual-cycle phase (if tracked), active environmental confounders (heat / air quality / pollen) and the deterministic findings: anomalies vs 30-day baseline, composite recovery strain, overnight physiology, FDR-significant behaviour impacts, 14-day trends. `include_findings=false` skips the scan. |
| `get_evidence(rule)` | Full evidence record for one knowledge-base rule: citation, research summary, tier, claim strength, measurement confidence, confounders. An unknown name returns the rule list. Lets the model cite research accurately instead of from memory. |
| query tools | Every tool in `tools/query_tools.py` (`get_daily_metrics`, `get_my_baselines`, `get_sleep_data`, `get_overnight_physiology`, `get_environment_data`, `scan_all_anomalies`, …) minus the write tools unless `--allow-writes`. |

### Prompts

`morning`, `midday`, `evening`, `night`, `weekly`, `general`. Each is built
**live** when requested: today's context + the deterministic findings, then the
same scan request the web app's AI Health Scan uses — so the client model
starts from code-computed anomalies rather than re-deriving them.

### Resources

| URI | Contents |
|---|---|
| `garmin://context/today` | Same as `get_current_context` |
| `garmin://knowledge/{rule}` | Same as `get_evidence` |
| `garmin://baselines` | 30-day personal baselines |
| `garmin://daily/recent` | Cached daily summaries, last 14 complete days |
| `garmin://profile` | `whoami` (user id, name, sex) + saved profile notes |

## Connecting a client

The Pi serves Streamable HTTP on the LAN. Merge these entries into your
existing `mcpServers` object — templates live in
[`garmin-insights/deploy/`](../garmin-insights/deploy/).

**Cursor** — `%USERPROFILE%\.cursor\mcp.json`
([template](../garmin-insights/deploy/cursor-mcp-remote.example.json)):

```json
"garmin-dan":   { "url": "http://192.168.4.148:8765/mcp" },
"garmin-helen": { "url": "http://192.168.4.148:8766/mcp" }
```

**Antigravity** — Agent → ⋯ → MCP Servers → Manage → View raw config
([template](../garmin-insights/deploy/antigravity-mcp-remote.example.json)):

```json
"garmin-dan":   { "serverUrl": "http://192.168.4.148:8765/mcp" },
"garmin-helen": { "serverUrl": "http://192.168.4.148:8766/mcp" }
```

**Claude Desktop** can't take a `url` directly; bridge with `mcp-remote`
(needs Node.js):

```json
"garmin-dan":   { "command": "npx", "args": ["-y", "mcp-remote", "http://192.168.4.148:8765/mcp"] },
"garmin-helen": { "command": "npx", "args": ["-y", "mcp-remote", "http://192.168.4.148:8766/mcp"] }
```

On the Windows MSIX build, "Edit Config" may open `%APPDATA%\Claude\…` while
the app actually reads
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json`
— if tools never appear, put the entries there too and fully quit Claude.

Reload MCP in the client; both servers should go green. Then e.g. *"Using
garmin-helen, how was last night's sleep vs baseline?"* or pick the `morning`
prompt.

### Alternative: stdio (local or over SSH)

Without the HTTP services, a client can launch the server itself:

```json
"garmin-dan": {
  "command": "ssh",
  "args": ["-T", "dan@192.168.4.148",
           "cd /home/dan/garmin-data && exec .venv/bin/garmin-insights-mcp --user dan"]
}
```

SSH must be key-based (MCP can't type a password). On the Pi itself use
`"command": "/home/dan/garmin-data/.venv/bin/garmin-insights-mcp", "args": ["--user", "dan"]`
with `cwd` set to the repo ([template](../garmin-insights/deploy/cursor-mcp.example.json),
[SSH template](../garmin-insights/deploy/cursor-mcp-windows-ssh.example.json)).
Each stdio launch rebuilds the 90-day cache, so the first call is slower.

## Running it on the Pi

```bash
pip install -e "garmin-insights[mcp]"          # mcp>=2

sudo systemctl status  garmin-insights-mcp@dan garmin-insights-mcp@helen
sudo systemctl restart garmin-insights-mcp@dan garmin-insights-mcp@helen
journalctl -u 'garmin-insights-mcp@*' -f
```

Restart after any Python change — the instructions are built at startup.
Files: `deploy/garmin-insights-mcp@.service`, `deploy/mcp-dan.env`,
`deploy/mcp-helen.env`. After editing the unit:

```bash
sudo cp deploy/garmin-insights-mcp@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart garmin-insights-mcp@dan garmin-insights-mcp@helen
```

CLI flags: `--user <id>`, `--transport stdio|streamable-http|sse`,
`--host`, `--port`, `--allowed-host <host:port>` (repeatable; DNS-rebinding
protection only accepts listed Host headers — the unit lists the Pi's LAN IP,
`pi5`, and localhost), `--allow-writes`, `-v`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Answers about the wrong person | Check the port ↔ entry mapping above; ask the model to call `get_current_context` — it names the user |
| HTTP 421 / "Invalid Host header" | Add the hostname you connect with via `--allowed-host` in the unit |
| `Unknown user 'dan'` | The root `.env` needs `USERS=dan:…,helen:…` |
| Timeout from Windows | `systemctl status` on the Pi; confirm the Pi's IP (`hostname -I`) |
| Claude Desktop: config saved, no tools | Use the MSIX virtualized config path above |
| Stale context (yesterday's date) | Shouldn't happen — date/cycle/findings are computed per call; only the static instructions need a restart |

Smoke test from any LAN machine:

```bash
curl -s -X POST http://192.168.4.148:8765/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

The response's `serverInfo.name` should be `garmin-insights-dan`.

## Privacy

The servers have no auth of their own — like the web dashboard, they are meant
for the LAN / Tailscale only; never expose the ports to the internet. Tool
results leave the Pi into the client, and from there to that client's model
provider (Cursor, Anthropic, Google).
