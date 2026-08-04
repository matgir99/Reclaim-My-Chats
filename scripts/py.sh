#!/usr/bin/env bash
# Shared helpers for ReclaimMyChats shell scripts.

# Pick the newest installed python3.x interpreter (e.g. python3.14 wins
# over python3.12). Override with PYTHON=<path>.
# Portable: uses POSIX sort numeric keys (works on GNU + BSD/macOS).
pick_python() {
    if [[ -n "${PYTHON:-}" ]]; then
        echo "$PYTHON"
        return
    fi
    local c
    for c in $(compgen -c python3. 2>/dev/null | grep -E '^python3\.[0-9]+$' | sort -t. -k2,2nr); do
        if command -v "$c" >/dev/null 2>&1; then
            echo "$c"
            return
        fi
    done
    command -v python3 >/dev/null 2>&1 && { echo "python3"; return; }
    echo "python3"
}

# Kill only the scraper's own Chrome/Chromium (identified by our profile
# path on the command line) — never the user's normal browser.
# No-op when pkill is unavailable (e.g. Git Bash).
kill_scraper_browser() {
    if command -v pkill >/dev/null 2>&1; then
        pkill -f playwright-profile 2>/dev/null
    fi
}
