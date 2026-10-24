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
# FRI_OFFLINE_CACHE, FRI_VENV (default .venv), OFFLINE_ISOLATION=python to accept the Python-only
# guard (best effort) inside an outer network-denied sandbox. Bash 3.2 compatible (macOS /bin/bash).
# Nothing runs Python with the caller's environment before `check` clears it.
set -euo pipefail

FRI=$(cd "$(dirname "$0")/.." && pwd)
MODE=${1:-check}
CACHE_ROOT=${FRI_OFFLINE_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/fri-offline}
GENE_REPO=${GENE_REPO:-$FRI/../gene}
FRI_VENV=${FRI_VENV:-$FRI/.venv}
# Deny outbound IP, and the system resolver's socket: macOS resolves names through mDNSResponder,
# which an IP-only rule does not cover.
PROFILE='(version 1)(allow default)(deny network-outbound (remote ip "*:*"))(allow network-outbound (remote ip "localhost:*"))(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'

die() { echo "offline-checks: $*" >&2; exit 1; }

if [ -z "${GENE_SHA:-}" ]; then
  # Parsed without Python: no interpreter starts before the environment is cleared.
  GENE_SHA=$(sed -n 's/.*"verified_controller_commit"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{40\}\)".*/\1/p' \
    "$FRI/docs/integration-pair.json" | head -n 1)
fi
case $GENE_SHA in
  *[!0-9a-f]* | '') die "GENE_SHA must be a full 40-character lowercase hex commit, got: $GENE_SHA" ;;
esac
[ ${#GENE_SHA} -eq 40 ] || die "GENE_SHA must be a full 40-character lowercase hex commit, got: $GENE_SHA"
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
"""offline-checks guard: deny non-loopback network and name lookups from Python's socket layer.

Best effort: native code and non-Python subprocesses are covered only by an OS-level sandbox.
"""
import functools
import ipaddress
import socket
import sys

_LOCAL = {None, '', 'localhost', '127.0.0.1', '::1', '0.0.0.0', '::'}
_SENDS = ('socket.connect', 'socket.sendto', 'socket.sendmsg')
_LOOKUPS = ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo')


def _host(value):
    if isinstance(value, tuple):
        value = value[0] if value else None
    return value.decode() if isinstance(value, bytes) else value


def _hook(event, args):
    if event in _SENDS:
        sock, address = args[0], args[-1]
        if (getattr(sock, 'family', None) in (socket.AF_INET, socket.AF_INET6) and
                isinstance(address, tuple) and _host(address) not in _LOCAL):
            raise PermissionError(f'offline-checks: outbound network denied ({event}, {address[0]})')
    elif event in _LOOKUPS and _host(args[0]) not in _LOCAL:
        raise PermissionError(f'offline-checks: name lookup denied ({event}, {_host(args[0])})')


sys.addaudithook(_hook)


def _numeric(host):
    try:
        ipaddress.ip_address(str(host).split('%', 1)[0])
        return True
    except ValueError:
        return False


def _address(name, args):
    if name in ('connect', 'connect_ex', 'bind'):
        return args[0] if args else None
    if name == 'sendto':
        return args[-1] if len(args) >= 2 else None
    return args[3] if len(args) >= 4 else None  # sendmsg(buffers, ancdata, flags, address)


def _checked(name):
    """CPython resolves hostnames in C before the audit event; check them in Python first."""
    real = getattr(socket.socket, name)

    @functools.wraps(real)
    def method(self, *args):
        address = _address(name, args)
        if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            host = _host(address)
            if host not in _LOCAL and (name != 'bind' or not _numeric(host)):
                raise PermissionError(f'offline-checks: outbound network denied (socket.{name}, {host})')
        return real(self, *args)
    return method


for _name in ('connect', 'connect_ex', 'sendto', 'sendmsg', 'bind'):
    setattr(socket.socket, _name, _checked(_name))
PY
}

choose_isolation() {
  SANDBOX=()
  if [ "${OFFLINE_ISOLATION:-auto}" = python ]; then
    ISOLATION='python-guard only (best effort; relies on an outer OS sandbox for full denial)'
    return
  fi
  if [ "$(uname)" = Darwin ] && /usr/bin/sandbox-exec -p "$PROFILE" /usr/bin/true 2>/dev/null; then
    ISOLATION='sandbox-exec + python-guard'
    SANDBOX=(/usr/bin/sandbox-exec -p "$PROFILE")
  elif [ "$(uname)" = Linux ] && command -v unshare >/dev/null && unshare -rn true 2>/dev/null; then
    ISOLATION='unshare -rn + python-guard'
    SANDBOX=(unshare -rn)
  else
    die 'no OS-level network sandbox is available here (sandbox-exec / unshare -rn). Run inside an outer
network-denied sandbox and set OFFLINE_ISOLATION=python to accept the Python-only guard as best effort.'
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

# Targets are RFC 5737/6761 test addresses that never reach a real host.
CANARY='
import errno, socket, sys
layer = sys.argv[1]
TEST_IP, TEST_NAME = "198.51.100.1", "offline-canary.invalid"
def udp():
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
def tcp():
    sock = socket.socket()
    sock.settimeout(3)
    return sock
probes = {"connect": lambda: tcp().connect((TEST_IP, 443))}
if layer == "guard":
    class MissedByGuard(Exception):
        pass
    def backstop(event, args):  # Added after the guard: a miss stops here, before any traffic.
        if event.startswith("socket.") and event not in ("socket.__new__", "socket.bind"):
            raise MissedByGuard(event)
    sys.addaudithook(backstop)
    probes.update({
        "sendto": lambda: udp().sendto(b"x", (TEST_IP, 9)),
        "sendmsg": lambda: udp().sendmsg([b"x"], [], 0, (TEST_IP, 9)),
        "getaddrinfo": lambda: socket.getaddrinfo(TEST_NAME, 443),
        "gethostbyname": lambda: socket.gethostbyname(TEST_NAME),
        "gethostbyname_ex": lambda: socket.gethostbyname_ex(TEST_NAME),
        "gethostbyaddr": lambda: socket.gethostbyaddr(TEST_IP),
        "getnameinfo": lambda: socket.getnameinfo((TEST_IP, 443), 0),
        # Hostnames passed to socket methods are resolved before any audit event fires.
        "connect(name)": lambda: tcp().connect((TEST_NAME, 443)),
        "connect_ex(name)": lambda: tcp().connect_ex((TEST_NAME, 443)),
        "sendto(name)": lambda: udp().sendto(b"x", (TEST_NAME, 9)),
        "sendmsg(name)": lambda: udp().sendmsg([b"x"], [], 0, (TEST_NAME, 9)),
        "bind(name)": lambda: tcp().bind((TEST_NAME, 0)),
    })
for name, probe in probes.items():
    try:
        probe()
    except OSError as exc:
        guard = isinstance(exc, PermissionError) and "offline-checks" in str(exc)
        denied = isinstance(exc, PermissionError) or exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH)
        if not (guard if layer == "guard" else denied):
            print(name, "inconclusive, not a denial by this layer:", exc); sys.exit(2)
        print(name, "blocked")
    except Exception as exc:
        print(name, "NOT DENIED by this layer:", type(exc).__name__, exc); sys.exit(1)
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
