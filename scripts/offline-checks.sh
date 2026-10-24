#!/usr/bin/env bash
# Offline verification shared by maintainers and review agents.
#
#   scripts/offline-checks.sh setup   # once per Gene commit; the only step that uses the network
#   scripts/offline-checks.sh [check] # credential-free, network-denied: tests, integration, ruff
#   scripts/offline-checks.sh all     # setup, then check
#
# The paired controller is the exact Gene commit in docs/integration-pair.json
# (verified_controller_commit), extracted with `git archive` and given its own venv from that
# commit's uv.lock. Environment overrides: GENE_SHA, GENE_REPO (default ../gene),
# FRI_OFFLINE_CACHE, FRI_VENV (default .venv), OFFLINE_ISOLATION=python to skip the OS sandbox.
# Bash 3.2 compatible (macOS /bin/bash).
set -euo pipefail

FRI=$(cd "$(dirname "$0")/.." && pwd)
MODE=${1:-check}
CACHE_ROOT=${FRI_OFFLINE_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/fri-offline}
GENE_REPO=${GENE_REPO:-$FRI/../gene}
FRI_VENV=${FRI_VENV:-$FRI/.venv}
PROFILE='(version 1)(allow default)(deny network-outbound (remote ip "*:*"))(allow network-outbound (remote ip "localhost:*"))'

die() { echo "offline-checks: $*" >&2; exit 1; }

json_python() {
  if [ -x "$FRI_VENV/bin/python" ]; then echo "$FRI_VENV/bin/python"; else command -v python3 || die 'python3 not found'; fi
}

if [ -z "${GENE_SHA:-}" ]; then
  GENE_SHA=$("$(json_python)" -c 'import json, sys; print(json.load(open(sys.argv[1]))["verified_controller_commit"])' \
    "$FRI/docs/integration-pair.json")
fi
echo "$GENE_SHA" | grep -Eq '^[0-9a-f]{40}$' || die "GENE_SHA must be a full 40-character commit, got: $GENE_SHA"
GENE_DIR=$CACHE_ROOT/$GENE_SHA
GENE_SRC=$GENE_DIR/src
GENE_VENV=$GENE_DIR/venv
READY=$GENE_DIR/.ready

setup() {
  echo "setup: may use the network for dependency downloads (uv); no credentials are read."
  command -v uv >/dev/null || die 'uv is required (https://docs.astral.sh/uv/)'
  (cd "$FRI" && UV_PROJECT_ENVIRONMENT="$FRI_VENV" uv sync --frozen)
  git -C "$GENE_REPO" cat-file -e "$GENE_SHA^{commit}" 2>/dev/null ||
    die "Gene commit $GENE_SHA is not in $GENE_REPO; fetch it first: git -C $GENE_REPO fetch origin"
  if [ -f "$READY" ] && [ "$(cat "$READY")" = "$GENE_SHA" ]; then
    echo "setup: Gene $GENE_SHA already prepared in $GENE_DIR"
    return
  fi
  rm -rf "$GENE_DIR"  # A partial earlier build has no .ready marker; rebuild it in place.
  mkdir -p "$GENE_SRC"
  git -C "$GENE_REPO" archive "$GENE_SHA" | tar -x -C "$GENE_SRC"
  UV_PROJECT_ENVIRONMENT="$GENE_VENV" uv sync --frozen --extra red --project "$GENE_SRC"
  echo "$GENE_SHA" > "$READY"
  echo "setup: Gene $GENE_SHA prepared in $GENE_DIR"
}

write_guard() {
  # Loaded via PYTHONPATH by every Python process in the check phase, alongside any OS sandbox.
  cat > "$1/sitecustomize.py" <<'PY'
"""offline-checks guard: deny non-loopback network and DNS from Python processes."""
import socket
import sys

_LOCAL = {None, '', 'localhost', '127.0.0.1', '::1'}


def _host(value):
    return value.decode() if isinstance(value, bytes) else value


def _hook(event, args):
    if event in ('socket.connect', 'socket.sendto'):
        sock, address = args[0], args[-1]
        if (getattr(sock, 'family', None) in (socket.AF_INET, socket.AF_INET6) and
                isinstance(address, tuple) and _host(address[0]) not in _LOCAL):
            raise PermissionError(f'offline-checks: outbound network denied ({address[0]})')
    elif event == 'socket.getaddrinfo' and _host(args[0]) not in _LOCAL:
        raise PermissionError(f'offline-checks: DNS lookup denied ({args[0]})')


sys.addaudithook(_hook)
PY
}

choose_isolation() {
  ISOLATION=python-guard
  SANDBOX=()
  if [ "${OFFLINE_ISOLATION:-auto}" = python ]; then
    return
  fi
  if [ "$(uname)" = Darwin ] && /usr/bin/sandbox-exec -p "$PROFILE" /usr/bin/true 2>/dev/null; then
    ISOLATION='sandbox-exec + python-guard'
    SANDBOX=(/usr/bin/sandbox-exec -p "$PROFILE")
  elif [ "$(uname)" = Linux ] && command -v unshare >/dev/null && unshare -rn true 2>/dev/null; then
    ISOLATION='unshare -rn + python-guard'
    SANDBOX=(unshare -rn)
  fi
}

# run PYTHONPATH SANDBOXED(0|1) COMMAND...: cleared environment, temporary home/config.
run() {
  local pythonpath=$1 sandboxed=$2
  shift 2
  local prefix=()
  if [ "$sandboxed" = 1 ]; then
    prefix=(${SANDBOX[@]+"${SANDBOX[@]}"})
  fi
  env -i PATH=/usr/bin:/bin HOME="$WORK/home" TMPDIR="$WORK/tmp" XDG_CONFIG_HOME="$WORK/config" \
    XDG_CACHE_HOME="$WORK/cache" PYTHONDONTWRITEBYTECODE=1 RUFF_NO_CACHE=true PYTHONPATH="$pythonpath" \
    ${prefix[@]+"${prefix[@]}"} "$@"
}

CANARY='
import errno, socket, sys
layer = sys.argv[1]
probes = [("connect", lambda: socket.socket().connect(("1.1.1.1", 443)))]
if layer == "guard":
    probes.append(("dns", lambda: socket.getaddrinfo("example.com", 443)))
for name, probe in probes:
    try:
        probe()
    except OSError as exc:
        guard = isinstance(exc, PermissionError) and "offline-checks" in str(exc)
        denied = isinstance(exc, PermissionError) or exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH)
        if not (guard if layer == "guard" else denied):
            print(name, "inconclusive, not a denial by this layer:", exc); sys.exit(2)
        print(name, "blocked:", exc)
    else:
        print(name, "SUCCEEDED: network is not blocked"); sys.exit(1)
'

canaries() {
  # Prove each layer every run: the Python guard alone, then the OS sandbox alone.
  run "$WORK/guard" 0 "$FRI_VENV/bin/python" -c "$CANARY" guard | sed 's/^/canary (python-guard): /' ||
    die 'the Python network guard did not deny an outbound connection'
  if [ ${#SANDBOX[@]} -gt 0 ]; then
    run '' 1 "$FRI_VENV/bin/python" -c "$CANARY" os | sed 's/^/canary (os sandbox): /' ||
      die 'the OS sandbox did not deny an outbound connection'
  fi
}

check() {
  [ -f "$READY" ] && [ "$(cat "$READY")" = "$GENE_SHA" ] ||
    die "Gene $GENE_SHA is not prepared; run: scripts/offline-checks.sh setup"
  [ -x "$FRI_VENV/bin/python" ] || die "no Fri environment at $FRI_VENV; run: scripts/offline-checks.sh setup"
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/fri-offline.XXXXXX")
  trap 'rm -rf "$WORK"' EXIT
  mkdir -p "$WORK/home" "$WORK/tmp" "$WORK/config" "$WORK/cache" "$WORK/guard" "$WORK/logs"
  write_guard "$WORK/guard"
  choose_isolation
  echo "check: Gene $GENE_SHA, isolation: $ISOLATION, no credentials in environment"
  canaries
  cd "$FRI"
  local failed=0 name
  suite() {
    name=$1
    shift
    if "$@" > "$WORK/logs/$name.log" 2>&1; then
      printf '%-12s ok      %s\n' "$name" "$(tail -n 1 "$WORK/logs/$name.log")"
    else
      printf '%-12s FAILED  %s\n' "$name" "$(tail -n 1 "$WORK/logs/$name.log")"
      sed 's/^/    /' "$WORK/logs/$name.log" | tail -n 60
      failed=1
    fi
  }
  suite unit run "$WORK/guard" 1 "$FRI_VENV/bin/python" -m pytest -q -p no:cacheprovider tests
  suite integration run "$WORK/guard:$FRI/src:$FRI/integration:$GENE_SRC/src:$GENE_SRC" 1 \
    "$GENE_VENV/bin/python" -m pytest -q -p no:cacheprovider integration/compat_tests integration/replay
  suite ruff run '' 1 "$FRI_VENV/bin/ruff" check --no-cache .
  [ "$failed" = 0 ] || die 'one or more offline checks failed'
  echo 'check: all offline checks passed'
}

case "$MODE" in
  setup) setup ;;
  check) check ;;
  all) setup; check ;;
  *) die "unknown mode: $MODE (use setup, check or all)" ;;
esac
