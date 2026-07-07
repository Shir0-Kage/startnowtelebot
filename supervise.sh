#!/usr/bin/env bash
# External supervisor for the StartNOW! bot.
#
# Keeps the bot alive through BOTH crashes and freezes. Freeze detection is
# EXTERNAL: this script (a separate process) watches the bot's heartbeat file,
# which the bot rewrites every second while its event loop is alive. If the
# heartbeat goes stale the bot is frozen — even a GIL-holding C extension that
# defeats the bot's in-process watchdog cannot stop THIS process or block a
# kernel-level `kill -9`. So no freeze ever needs a human again.
#
# Run it under tmux so it survives disconnects:
#     tmux new -s bot        # then:
#     bash supervise.sh
# Stop everything: Ctrl+C (or `tmux kill-session -t bot`).
#
# Tunables (env): PYTHON, HEARTBEAT_FILE, STALL_LIMIT (seconds), RESTART_DELAY.

set -u
cd "$(dirname "$0")"

PY="${PYTHON:-.venv/bin/python}"
HEARTBEAT="${HEARTBEAT_FILE:-heartbeat}"
STALL_LIMIT="${STALL_LIMIT:-45}"       # seconds with no heartbeat => frozen
RESTART_DELAY="${RESTART_DELAY:-3}"
BOT_PID=""

log() { echo "[supervise] $(date '+%F %T') $*"; }

stop() { [ -n "$BOT_PID" ] && kill -9 "$BOT_PID" 2>/dev/null; log "stopped."; exit 0; }
trap stop INT TERM

start() {
    rm -f "$HEARTBEAT"
    "$PY" main.py &
    BOT_PID=$!
    STARTED=$(date +%s)
    log "started bot (pid $BOT_PID)"
}

start
while true; do
    sleep 5

    # crash recovery: process gone -> relaunch
    if ! kill -0 "$BOT_PID" 2>/dev/null; then
        log "bot exited — restarting in ${RESTART_DELAY}s"
        sleep "$RESTART_DELAY"; start; continue
    fi

    # freeze recovery: heartbeat stale -> kill -9 + relaunch. Before the first
    # heartbeat appears we measure from start time, so a startup hang is caught too.
    now=$(date +%s)
    if [ -f "$HEARTBEAT" ]; then
        last=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo "$now")
    else
        last="$STARTED"
    fi
    age=$(( now - last ))
    if [ "$age" -ge "$STALL_LIMIT" ]; then
        log "FROZEN — no heartbeat for ${age}s (limit ${STALL_LIMIT}s). kill -9 $BOT_PID and restart"
        kill -9 "$BOT_PID" 2>/dev/null
        wait "$BOT_PID" 2>/dev/null
        sleep 1; start
    fi
done
