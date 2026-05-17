#!/usr/bin/env bash
# Launch the Garmin fetcher + insights web server for a single user.
# Usage: run-user.sh <username>
#   - reads /home/dan/garmin-data/users/<username>.env
#   - logs to   /home/dan/garmin-data/logs/<username>-{fetch,web}.log
# Designed for cron @reboot: backgrounds both processes and exits.

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

TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$TS] ---- starting ${USER_NAME} ----" >> "${LOG_DIR}/${USER_NAME}-fetch.log"
echo "[$TS] ---- starting ${USER_NAME} ----" >> "${LOG_DIR}/${USER_NAME}-web.log"

# Fetcher: infinite poll loop
nohup "${VENV_BIN}/python" -m garmin_grafana.garmin_fetch \
    >> "${LOG_DIR}/${USER_NAME}-fetch.log" 2>&1 &

# Web server: long-running daemon on the port from the env file
nohup "${VENV_BIN}/garmin-insights" web \
    >> "${LOG_DIR}/${USER_NAME}-web.log" 2>&1 &

disown -a
