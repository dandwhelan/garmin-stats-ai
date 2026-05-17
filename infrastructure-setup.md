# Infrastructure Setup

How the garmin-stats-ai project runs on the Raspberry Pi 5 (`pi5`).

## Process model

Two long-running background processes per user:

| Process | Command | Purpose |
|---------|---------|---------|
| Fetcher | `python -m garmin_grafana.garmin_fetch` | Polls Garmin Connect every **5 minutes**, writes data to SQLite |
| Web server | `garmin-insights web` | FastAPI + SSE chat interface |

## Crontab

Both processes are managed via crontab (user `dan`). The scripts self-heal — if a process is already running they skip it, so it's safe to re-run at any time.

```cron
# Start on reboot (staggered to avoid race conditions)
@reboot  sleep 20 && bash /home/dan/garmin-data/scripts/run-dan.sh   >> /home/dan/garmin-data/logs/cron.log 2>&1
@reboot  sleep 25 && bash /home/dan/garmin-data/scripts/run-helen.sh >> /home/dan/garmin-data/logs/cron.log 2>&1

# Watchdog: restart either process if it has crashed
*/10 * * * *  bash /home/dan/garmin-data/scripts/run-dan.sh   >> /home/dan/garmin-data/logs/cron.log 2>&1
*/10 * * * *  bash /home/dan/garmin-data/scripts/run-helen.sh >> /home/dan/garmin-data/logs/cron.log 2>&1
```

`scripts/run-dan.sh` and `scripts/run-helen.sh` both call `scripts/run-user.sh <name>`, which:
1. Sources the user's env file (`users/<name>.env`)
2. Checks if the fetcher/web server are alive by scanning `/proc/<pid>/environ` for that user's `SQLITE_DB_PATH`
3. Starts whichever processes aren't running

## Users

| User | DB | Web port | Garmin token dir |
|------|-----|----------|-----------------|
| Dan | `dan.db` | 8080 | `~/.garminconnect` |
| Helen | `helen.db` | 8081 | `~/.garminconnect-helen` |

Each user has their own env file at `users/<name>.env` containing `GARMINCONNECT_EMAIL`, `GARMINCONNECT_PASSWORD`, `SQLITE_DB_PATH`, `ANTHROPIC_API_KEY`, and `WEB_PORT`.

## Logs

```
logs/
  cron.log        # watchdog output (start/skip messages)
  dan-fetch.log   # Garmin fetch activity for Dan
  dan-web.log     # web server output for Dan
  helen-fetch.log # Garmin fetch activity for Helen
  helen-web.log   # web server output for Helen
```

## Backfilling missing data

The fetcher detects new data by comparing the latest heart rate timestamp in the DB against the watch's last upload time. If it loses track (e.g. after a restart), it falls back to re-fetching the last 7 days. For gaps older than 7 days, use the backfill script:

```bash
cd /home/dan/garmin-data
source .venv/bin/activate
set -a && source users/helen.env && set +a
python backfill_dates.py 2026-05-01 2026-05-08
```

Note: if a date shows no data even after a backfill, the data genuinely doesn't exist on Garmin Connect (e.g. watch not worn that day).
