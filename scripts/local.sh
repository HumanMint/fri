#!/usr/bin/env bash
# A local trial of Fri in your own test server, against a local Gene controller (docs/quickstart.md).
#
#   scripts/local.sh setup               # check tools, build .venv, write .env.local, then check it
#   scripts/local.sh check [--controller] # validate .env.local without printing values;
#                                         # --controller also reads the controller's /fri/contract
#   scripts/local.sh run                  # check, then start Fri with .env.local
#
# setup keeps any values already in .env.local, such as the ones Gene's `scripts/dev.py fri-env` writes,
# and adds only missing settings. .env.local, .state/ and .secrets/ are git-ignored.
# Bash 3.2 compatible (macOS /bin/bash).
set -euo pipefail

FRI=$(cd "$(dirname "$0")/.." && pwd)
MODE=${1:-help}
[ $# -gt 0 ] && shift
PYTHON=$FRI/.venv/bin/python

die() { echo "local: $*" >&2; exit 1; }

helper() {
  [ -x "$PYTHON" ] || die 'no .venv yet; run: scripts/local.sh setup'
  (cd "$FRI" && "$PYTHON" scripts/local_env.py "$@")
}

setup() {
  command -v git >/dev/null || die 'git is required'
  command -v uv >/dev/null || die 'uv is required: https://docs.astral.sh/uv/getting-started/installation/'
  echo "setup: uv $(uv --version | awk '{print $2}'), git $(git --version | awk '{print $3}')"
  # uv downloads Python 3.11 (.python-version) when this machine doesn't have it.
  (cd "$FRI" && uv sync --frozen --quiet)
  echo "setup: .venv ready ($("$PYTHON" --version))"
  mkdir -p "$FRI/.state/fri-data" "$FRI/.secrets"
  chmod 700 "$FRI/.state" "$FRI/.state/fri-data" "$FRI/.secrets"
  helper init
  echo
  helper check || true  # A fresh .env.local still lists the values to fill in.
  cat <<'NEXT'

Next (docs/quickstart.md has the details):
  1. Fill in the values marked "missing" in .env.local; Gene's `scripts/dev.py fri-env` writes the
     controller's share of them.
  2. Connect Fri's model access: uv run --no-sync python scripts/connect_chatgpt.py
  3. Start the Gene controller, then: scripts/local.sh check --controller
  4. Start Fri: scripts/local.sh run
NEXT
}

case "$MODE" in
  setup) setup ;;
  check) helper check "$@" ;;
  run) helper run "$@" ;;
  help | -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command: $MODE (use setup, check or run)" ;;
esac
