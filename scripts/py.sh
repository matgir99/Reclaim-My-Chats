#!/usr/bin/env bash
# Shared helpers for ReclaimMyChats shell scripts.

# Pick the newest installed python3.x interpreter (e.g. python3.14 wins
# over python3.12). Override with PYTHON=<path>.
pick_python() {
    if [[ -n "${PYTHON:-}" ]]; then
        echo "$PYTHON"
        return
    fi
    # Newest python3.N found on PATH (version-sorted, descending).
    local c
    for c in $(compgen -c python3. 2>/dev/null | grep -E '^python3\.[0-9]+$' | sort -Vr); do
        if command -v "$c" >/dev/null 2>&1; then
            echo "$c"
            return
        fi
    done
    command -v python3 >/dev/null 2>&1 && { echo "python3"; return; }
    echo "python3"
}
