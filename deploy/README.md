# Raspberry Pi deployment

## Services overview

| File | Purpose |
|------|---------|
| `garmin-insights.service` | Runs the web server on boot (one instance, all users) |
| `garmin-fetch@.service` | Template — one running instance per Garmin account |

---

## 1. Install the web server

```bash
sudo cp /home/pi/garmin-stats-ai/deploy/garmin-insights.service \
        /etc/systemd/system/garmin-insights.service

sudo systemctl daemon-reload
sudo systemctl enable garmin-insights
sudo systemctl start garmin-insights

# Check / tail logs
sudo systemctl status garmin-insights
journalctl -u garmin-insights -f
```

---

## 2. Set up the Garmin data fetchers (one per account)

The fetcher can only hold one set of Garmin credentials per process. For two
accounts, run two separate services — the `garmin-fetch@.service` template
makes this painless.

### 2a. Create per-user env files

```bash
mkdir -p /home/pi/garmin-stats-ai/users
mkdir -p /home/pi/garmin-data          # where the SQLite DBs live

# Copy and edit for each person
cp /home/pi/garmin-stats-ai/users/alice.env.example \
   /home/pi/garmin-stats-ai/users/alice.env
nano /home/pi/garmin-stats-ai/users/alice.env

cp /home/pi/garmin-stats-ai/users/bob.env.example \
   /home/pi/garmin-stats-ai/users/bob.env
nano /home/pi/garmin-stats-ai/users/bob.env
```

Each `.env` needs at minimum:

```bash
GARMINCONNECT_EMAIL=person@example.com
GARMINCONNECT_PASSWORD=their_password
SQLITE_DB_PATH=/home/pi/garmin-data/person.db
TOKEN_DIR=/home/pi/.garminconnect-person   # MUST differ per user
```

> **`TOKEN_DIR` must be unique per user.** The fetcher caches auth tokens
> there. If two users share the same directory their tokens will overwrite
> each other and both will get logged out.

### 2b. Install and start the template service

```bash
sudo cp /home/pi/garmin-stats-ai/deploy/garmin-fetch@.service \
        /etc/systemd/system/garmin-fetch@.service

sudo systemctl daemon-reload

# The part after @ becomes %i in the unit — matches the filename in users/
sudo systemctl enable garmin-fetch@alice
sudo systemctl enable garmin-fetch@bob
sudo systemctl start garmin-fetch@alice
sudo systemctl start garmin-fetch@bob

# Check each
sudo systemctl status garmin-fetch@alice
sudo systemctl status garmin-fetch@bob
journalctl -u garmin-fetch@alice -f
```

---

## 3. Configure the web server to know both DBs

In `/home/pi/garmin-stats-ai/.env`:

```bash
# Anthropic key
ANTHROPIC_API_KEY=sk-ant-...

# Comma-separated  user_id:db_path  pairs — must match the SQLITE_DB_PATH
# values in the per-user fetcher env files above.
USERS=alice:/home/pi/garmin-data/alice.db,bob:/home/pi/garmin-data/bob.db

# Web server
WEB_HOST=0.0.0.0
WEB_PORT=8080
```

Restart the web server after changing this:

```bash
sudo systemctl restart garmin-insights
```

The header on the dashboard will show a user picker. Selecting a user reloads
all charts and starts a fresh chat session scoped to that user's data.

---

## How data isolation works

- Each fetcher process only ever touches **one** Garmin account and **one** DB file.
- The fetchers run continuously in the background (default: poll every 5 min).
- The web server loads per-user `HealthAgent` instances lazily — agent for
  Alice only queries `alice.db`, agent for Bob only queries `bob.db`.
- Chat sessions are bound to the user that started them. Reusing a session id
  with a different user allocates a fresh session so no history leaks.

---

## Adding more users

1. Create `users/<name>.env` from the example template.
2. `sudo systemctl enable --now garmin-fetch@<name>`
3. Add `<name>:/home/pi/garmin-data/<name>.db` to `USERS` in `.env`.
4. `sudo systemctl restart garmin-insights`
