#!/usr/bin/env bash
# Convenience wrapper: runs `python -m reclaim <provider> [args...]` DETACHED
# in the background with a PID file and log, so long archive runs survive
# closing the terminal.
#
# The standard way to use ReclaimMyChats is the CLI directly, in the
# foreground — this script is only for runs you want in the background:
#   reclaim <provider> [TITLE] [options]      # standard CLI (see README)
#   ./run.sh <provider> [same options]        # same command, but detached
#
# Usage:
#   ./run.sh <googleaistudio|deepseek|kimi|chatgpt|claude|\
#            googlegemini|all> [args...]                       # start
#   ./run.sh progress                                             # PID + log tail
#   ./run.sh stop                                                 # stop + its Chrome
#
# Examples:
#   ./run.sh googleaistudio              # background update of AI Studio
#   ./run.sh all --rebuild               # rebuild all six providers
#   ./run.sh deepseek "latex" --log      # title-filtered, full log
#   ./run.sh progress                    # how is it going?
#
# Notes:
# * Every CLI option works here (--rebuild, --list, --log, --dry-run,
#   --skip, --limit, --no-raw, -o). Output goes to $LOGFILE; default
#   verbosity is summary-only, --log gives the full per-chat log.
# * `progress` is this script's own status (PID + last log lines). For the
#   offline archive overview use `reclaim status` instead.
# * First run of a provider opens a browser window for login (see README).
# * Never use `pkill -f <pattern>` where the pattern also appears in this
#   command line — pkill matches the invoking shell and kills it.
# * setsid detaches into a new session so the job survives the terminal;
#   macOS falls back to nohup alone (SIGHUP immunity).

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
    # setsid is Linux-only; macOS falls back to nohup alone.
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$PY" -u -m reclaim "$provider" "$@" \
            >> "$LOGFILE" 2>&1 < /dev/null &
    else
        nohup "$PY" -u -m reclaim "$provider" "$@" \
            >> "$LOGFILE" 2>&1 < /dev/null &
    fi
    echo $! > "$PIDFILE"
    echo "started '$provider' (PID $(cat "$PIDFILE")) — log: $LOGFILE"
    echo "watch: ./run.sh progress · stop: ./run.sh stop"
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
    googleaistudio|deepseek|kimi|chatgpt|claude|googlegemini|all)
        cmd_start "$@" ;;
    progress)          cmd_progress ;;
    stop)              cmd_stop ;;
    *) echo "usage: $0 {googleaistudio|deepseek|kimi|chatgpt|claude|googlegemini|all [args...]|progress|stop}"
       echo "  e.g. ./run.sh googleaistudio --rebuild | ./run.sh all | ./run.sh progress | ./run.sh stop"; exit 1 ;;
esac
