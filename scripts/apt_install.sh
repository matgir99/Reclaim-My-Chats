#!/usr/bin/env bash
# Install Ubuntu packages on a CI runner without letting a stalled package
# mirror use up the whole job.
#
# Usage: scripts/apt_install.sh PACKAGE...
#
# Fetching the package lists and downloading the packages is one attempt,
# limited to APT_ATTEMPT_LIMIT (default 4 minutes; it normally takes about one).
# A failed or stalled attempt is retried, up to APT_ATTEMPTS (default 3) in
# all, and from the second attempt on apt uses archive.ubuntu.com instead of
# the runner's own Azure mirror, which is the one seen stalling. Packages that
# finished downloading stay in apt's cache, so a retry continues where the last
# attempt stopped. Installing then runs from that cache without a time limit,
# so a limit never interrupts dpkg half-way.
#
# For tests: APT_GET (default apt-get), SUDO (default sudo) and APT_SOURCES
# (the source files to switch) replace the real commands and files.
set -uo pipefail

attempts=${APT_ATTEMPTS:-3}
limit=${APT_ATTEMPT_LIMIT:-4m}
sudo=${SUDO-sudo}
apt_get=${APT_GET:-apt-get}
read -r -a sources <<< "${APT_SOURCES:-/etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources}"

if [ $# -eq 0 ]; then
  echo "usage: $0 PACKAGE..." >&2
  exit 2
fi

apt() {
  $sudo env DEBIAN_FRONTEND=noninteractive "$apt_get" \
    -o Acquire::Retries=3 -o Acquire::http::Timeout=30 "$@"
}

fetch() {  # one attempt: the package lists, then the packages themselves
  apt update && apt install -y --download-only "$@"
}

use_main_mirror() {
  local file
  for file in "${sources[@]}"; do
    if [ -f "$file" ]; then
      $sudo sed -i 's#http://azure\.archive\.ubuntu\.com/#http://archive.ubuntu.com/#g' "$file"
    fi
  done
  echo "::notice::apt now uses archive.ubuntu.com instead of the runner's mirror"
}

export -f apt fetch
export sudo apt_get
for attempt in $(seq 1 "$attempts"); do
  if timeout --kill-after=20s "$limit" bash -c 'fetch "$@"' fetch "$@"; then
    exec bash -c 'apt install -y --no-download "$@"' install "$@"
  fi
  echo "::warning::apt attempt $attempt of $attempts failed or took longer than $limit"
  if [ "$attempt" -eq 1 ]; then
    use_main_mirror
  fi
done
echo "::error::could not download the packages in $attempts attempts"
exit 1
