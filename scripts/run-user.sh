#!/usr/bin/env bash
# Launch the Garmin fetcher + insights web server for a single user.
# Usage: run-user.sh <username>
#   - reads /home/dan/garmin-data/users/<username>.env
#   - logs to   /home/dan/garmin-data/logs/<username>-{fetch,web}.log
# Designed for cron @reboot: backgrounds both processes and exits.
#
# Safe to re-run: if a fetcher / web server for this user is already alive
# (matched by the user's SQLITE_DB_PATH appearing in the process environment),
# we skip launching a duplicate.  Re-running with no live processes will
# (re-)start both — so this script doubles as a self-heal/restart hook.

set -eu

USER_NAME="${1:?usage: run-user.sh <username>}"

REPO_ROOT="/home/dan/garmin-data"
VENV_BIN="${REPO_ROOT}/.venv/bin"
ENV_FILE="${REPO_ROOT}/users/${USER_NAME}.env"
LOG_DIR="${REPO_ROOT}/logs"

if [ ! -f "$ENV_FILE" ]; then
    echo "Env file not found: $ENV_FILE" >&2
    exit 1
fi
if [ ! -x "${VENV_BIN}/python" ]; then
    echo "Venv python not found: ${VENV_BIN}/python" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

# Export every var in the user's env file
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# Look for live processes that already have *this user's* DB path in their
# environment.  We match the env var rather than the username because two
# users could run the same command line on different DBs.
match_db="${SQLITE_DB_PATH:?SQLITE_DB_PATH not set in $ENV_FILE}"

is_alive() {
    # $1 = pattern to grep from /proc/<pid>/cmdline
    local cmd_pat="$1"
    local pid
    for pid in $(pgrep -f "$cmd_pat" || true); do
        # Read this PID's environ; if it has our SQLITE_DB_PATH, count it alive
        if tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null \
                | grep -qx "SQLITE_DB_PATH=${match_db}"; then
            echo "$pid"
            return 0
        fi
    done
    return 1
}

TS="$(date '+%Y-%m-%d %H:%M:%S')"

# Fetcher: infinite poll loop
if pid=$(is_alive "garmin_grafana.garmin_fetch"); then
    echo "[$TS] fetcher already running for ${USER_NAME} (pid $pid) — skipping" \
        >> "${LOG_DIR}/${USER_NAME}-fetch.log"
else
    echo "[$TS] ---- starting fetcher for ${USER_NAME} ----" \
        >> "${LOG_DIR}/${USER_NAME}-fetch.log"
    nohup "${VENV_BIN}/python" -m garmin_grafana.garmin_fetch \
        >> "${LOG_DIR}/${USER_NAME}-fetch.log" 2>&1 &
fi

# Web server: long-running daemon on the port from the env file
if pid=$(is_alive "garmin-insights web"); then
    echo "[$TS] web server already running for ${USER_NAME} (pid $pid) — skipping" \
        >> "${LOG_DIR}/${USER_NAME}-web.log"
else
    echo "[$TS] ---- starting web server for ${USER_NAME} ----" \
        >> "${LOG_DIR}/${USER_NAME}-web.log"
    nohup "${VENV_BIN}/garmin-insights" web \
        >> "${LOG_DIR}/${USER_NAME}-web.log" 2>&1 &
fi

disown -a
