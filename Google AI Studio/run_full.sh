#!/usr/bin/env bash
# Safe detached runner for the Google AI Studio scraper.
# Usage:
#   ./run_full.sh start [extra scraper args...]   # launch detached, write PID + log
#   ./run_full.sh status                          # show progress tail
#   ./run_full.sh stop                            # stop scraper AND its chrome
#
# Notes:
# * Never use `pkill -f chrome` or `pkill -f scrape...` — the pattern matches
#   the invoking shell's own command line and kills it (learned the hard way).
# * setsid detaches into a new session so the harness can't reap the job.

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/.scrape.pid"
LOGFILE="$DIR/scrape_full.log"
CHROME_MARK="playwright-profile"

cmd_start() {
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "already running (PID $(cat "$PIDFILE"))"; exit 1
    fi
    pkill chrome 2>/dev/null; sleep 2
    rm -f "$DIR/../.playwright-profile/Singleton"* 2>/dev/null
    : > "$LOGFILE"
    setsid nohup python3.14 "$DIR/scrape_googleaistudio.py" "$@" \
        >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    echo "started PID $(cat "$PIDFILE") — log: $LOGFILE"
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
    start)  shift; cmd_start "$@" ;;
    status) cmd_status ;;
    stop)   cmd_stop ;;
    *) echo "usage: $0 {start [args...]|status|stop}"; exit 1 ;;
esac
