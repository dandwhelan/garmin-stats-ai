# Raspberry Pi deployment

Two things live here:

1. `garmin-insights.service` — systemd unit that runs the web server on boot.
2. Instructions for enabling multi-user mode.

## Install the systemd service

```bash
# 1. Copy the unit file into place
sudo cp /home/pi/garmin-stats-ai/deploy/garmin-insights.service \
        /etc/systemd/system/garmin-insights.service

# 2. Reload + enable + start
sudo systemctl daemon-reload
sudo systemctl enable garmin-insights
sudo systemctl start garmin-insights

# 3. Check status / tail logs
sudo systemctl status garmin-insights
journalctl -u garmin-insights -f
```

Adjust `User=`, `WorkingDirectory=`, and `ExecStart=` paths in the unit if your
install lives elsewhere.

## Multi-user mode

The web app supports multiple users — each user has their own SQLite DB and
their own agent. Sessions are bound to the user that started them so chat
history and queries never cross.

### Configure users

In your `.env`:

```bash
# Comma-separated  user_id:db_path  entries
USERS=alice:/data/alice.db,bob:/data/bob.db

# Optional fallback (used when USERS is empty)
SQLITE_DB_PATH=/data/garmin.db
```

If `USERS` is empty, the app runs in single-user mode using `SQLITE_DB_PATH`
under the synthetic user id `default` and the picker is hidden in the UI.

### Fetching data per user

The `garmin-grafana` fetcher writes to a single DB at `SQLITE_DB_PATH`. To
populate each user's DB, run the fetcher once per user with that user's
credentials and DB path. Two separate cron / systemd timers work fine:

```bash
# Alice
GARMINCONNECT_EMAIL=alice@example.com \
GARMINCONNECT_PASSWORD=... \
SQLITE_DB_PATH=/data/alice.db \
  python -m garmin_grafana.garmin_fetch

# Bob
GARMINCONNECT_EMAIL=bob@example.com \
GARMINCONNECT_PASSWORD=... \
SQLITE_DB_PATH=/data/bob.db \
  python -m garmin_grafana.garmin_fetch
```

You can wrap each in its own systemd `*.service` + `*.timer` if you want them
on a schedule.

### How the web app enforces isolation

- `GET /api/users` lists configured user ids
- All data endpoints accept `?user=<id>` (or `"user": "<id>"` in POST bodies)
- The server validates the user against the configured `USERS` map and rejects
  unknown ids with 404
- Each chat session stores the `user_id` it was created for; if a session id is
  reused with a different user, a fresh session is allocated instead (so chat
  history from user A can never be sent to the agent reading user B's DB)
- Each user gets its own pooled `HealthAgent` instance, which only ever opens
  the DB path from its own `Settings`
