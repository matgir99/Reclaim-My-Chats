#!/usr/bin/env bash
# Generic safe runner for ReclaimMyChats providers.
# Usage:
#   ./run.sh <googleaistudio|deepseek|kimi|chatgpt|all> [args...]  # detached
#   ./run.sh progress                                             # live run progress
#   ./run.sh stop                                                 # stop + chrome
#
# Examples:
#   ./run.sh googleaistudio
#   ./run.sh all --rebuild
#   ./run.sh deepseek "latex"
#   ./run.sh progress
#
# Notes:
# * Args are forwarded to `python -m reclaim <provider> [args...]`; the full
#   CLI surface applies (--rebuild, --list, --log, --dry-run, --skip, ...).
# * `progress` = the detached run's progress (PID + log tail). For the
#   offline archive overview run `python -m reclaim status`.
# * Never use `pkill -f <pattern>` where the pattern also appears in this
#   command line — pkill matches the invoking shell and kills it.
# * setsid detaches into a new session so the job survives the terminal.

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/.reclaim.pid"
LOGFILE="$DIR/reclaim_run.log"

# shellcheck source=scripts/py.sh
source "$DIR/scripts/py.sh"
PY="$(pick_python)"

cmd_start() {
    local provider="$1"; shift
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "already running (PID $(cat "$PIDFILE"))"; exit 1
    fi
    kill_scraper_browser; sleep 2
    rm -f "$DIR/.playwright-profile/Singleton"* 2>/dev/null
    : > "$LOGFILE"
    # setsid is Linux-only; macOS falls back to nohup alone (still
    # survives terminal close via SIGHUP immunity).
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$PY" -u -m reclaim "$provider" "$@" \
            >> "$LOGFILE" 2>&1 < /dev/null &
    else
        nohup "$PY" -u -m reclaim "$provider" "$@" \
            >> "$LOGFILE" 2>&1 < /dev/null &
    fi
    echo $! > "$PIDFILE"
    echo "started '$provider' PID $(cat "$PIDFILE") — log: $LOGFILE"
}

cmd_progress() {
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
    kill_scraper_browser
    echo "stopped"
}

case "${1:-}" in
    googleaistudio|deepseek|kimi|chatgpt|all) cmd_start "$@" ;;
    progress)          cmd_progress ;;
    stop)              cmd_stop ;;
    *) echo "usage: $0 {googleaistudio|deepseek|kimi|chatgpt|all [args...]|progress|stop}"
       echo "  e.g. ./run.sh googleaistudio --rebuild | ./run.sh all | ./run.sh progress | ./run.sh stop"; exit 1 ;;
esac
