#!/usr/bin/env bash
# Generic safe runner for ReclaimMyChats scrape modes.
# Usage:
#   ./run.sh <aistudio|deepseek> [extra scraper args...]   # launch detached
#   ./run.sh status                                        # show progress
#   ./run.sh stop                                          # stop scraper + chrome
#
# Notes:
# * Never use `pkill -f <pattern>` where the pattern also appears in this
#   command line — pkill matches the invoking shell and kills it.
# * setsid detaches into a new session so the job survives the terminal.

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/.reclaim.pid"
LOGFILE="$DIR/reclaim_run.log"

cmd_start() {
    local provider="$1"; shift
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "already running (PID $(cat "$PIDFILE"))"; exit 1
    fi
    pkill chrome 2>/dev/null; sleep 2
    rm -f "$DIR/.playwright-profile/Singleton"* 2>/dev/null
    : > "$LOGFILE"
    setsid nohup "${PYTHON:-python3}" -u -m reclaim scrape "$provider" "$@" \
        >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    echo "started 'scrape $provider' PID $(cat "$PIDFILE") — log: $LOGFILE"
}

cmd_status() {
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "RUNNING (PID $(cat "$PIDFILE"))"
    else
        echo "not running"
    fi
    tail -n 15 "$LOGFILE" 2>/dev/null || echo "(no log yet)"
}

cmd_stop() {
    if [[ -f "$PIDFILE" ]]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null
        rm -f "$PIDFILE"
    fi
    pkill chrome 2>/dev/null
    echo "stopped"
}

case "${1:-}" in
    aistudio|deepseek) cmd_start "$@" ;;
    status)            cmd_status ;;
    stop)              cmd_stop ;;
    *) echo "usage: $0 {aistudio|deepseek [args...]|status|stop}"; exit 1 ;;
esac
