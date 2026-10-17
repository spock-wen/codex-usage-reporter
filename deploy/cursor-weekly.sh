#!/bin/sh
# Managed by codex-usage-reporter. Invoked by the existing Hermes task as root.
set -eu
LOCK=/run/lock/codex-usage-cursor-weekly.lock
exec 9>"$LOCK"
if ! flock -n 9; then
    echo '{"ok":false,"status":"already_running"}'
    exit 75
fi
exec timeout --signal=TERM --kill-after=30s 3600s /usr/bin/python3 \
    /opt/codex-usage/current/cursor_weekly.py "$@"
