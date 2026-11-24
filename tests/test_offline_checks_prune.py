"""offline-checks.sh cache pruning, run under bash against throwaway caches."""

import os
import re
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'offline-checks.sh'
OLD = time.time() - 3 * 86400


def functions():
    text = SCRIPT.read_text()
    return ''.join(re.search(rf'^{name}\(\) {{.*?^}}\n', text, re.M | re.S).group(0) for name in ('in_use', 'prune'))


def prune(root, current, keep=None, cwd=None):
    env = {'PATH': os.environ['PATH'], 'CACHE_ROOT': str(root), 'GENE_SHA': current}
    if keep is not None:
        env['FRI_OFFLINE_KEEP'] = keep
    program = 'set -euo pipefail\ndie() { echo "die: $*"; exit 1; }\n' + functions() + 'prune\n'
    return subprocess.run(['bash', '-c', program], env=env, cwd=cwd, capture_output=True, text=True)


def entry(root, n, age, ready=True):
    sha = f'{n:040x}'
    folder = root / sha
    (folder / 'venv').mkdir(parents=True)
    if ready:
        (folder / '.ready').write_text(sha + '\n')
    os.utime(folder, (age, age))
    return folder


def test_keeps_current_and_most_recent_and_skips_folders_setup_did_not_make(tmp_path):
    root = tmp_path / 'cache'
    folders = [entry(root, n, OLD - n * 60) for n in range(1, 15)]  # 1 is newest, 14 oldest
    current = entry(root, 15, OLD - 10**6)
    fresh = entry(root, 16, time.time() - 60)
    foreign = entry(root, 17, OLD - 10**7, ready=False)
    (root / 'not-a-commit').mkdir()
    result = prune(root, current.name)
    assert result.returncode == 0, result.stderr
    # fresh, 1..9 are the 10 most recent others; 10..14 go.
    assert [f.exists() for f in folders] == [True] * 9 + [False] * 5
    assert current.exists() and fresh.exists() and foreign.exists() and (root / 'not-a-commit').exists()


def test_an_odd_name_never_reaches_a_path_outside_the_cache(tmp_path):
    outside = entry(tmp_path, 99, OLD)  # Old, ready, and a sibling of the cache root.
    root = tmp_path / 'cache'
    current = entry(root, 1, time.time())
    (root / f'odd\n{outside.name}').mkdir()
    assert prune(root, current.name, keep='0', cwd=tmp_path).returncode == 0
    assert outside.exists() and current.exists()


def test_a_live_check_keeps_its_folder_and_a_dead_ones_marker_is_cleared(tmp_path):
    root = tmp_path / 'cache'
    current = entry(root, 1, time.time())
    held, stale = entry(root, 2, OLD), entry(root, 3, OLD)
    (held / f'.in-use.{os.getpid()}').write_text('')
    dead = subprocess.Popen(['true'])
    dead.wait()
    (stale / f'.in-use.{dead.pid}').write_text('')
    for folder in (held, stale):  # Writing a marker refreshes the folder's time; make both old again.
        os.utime(folder, (OLD, OLD))
    assert prune(root, current.name, keep='0').returncode == 0
    assert held.exists() and not (held / '.evicting').exists()
    assert not stale.exists()


def test_a_folder_already_claimed_for_eviction_is_left_to_its_claimer(tmp_path):
    root = tmp_path / 'cache'
    current = entry(root, 1, time.time())
    claimed = entry(root, 2, OLD)
    (claimed / '.evicting').mkdir()
    os.utime(claimed, (OLD, OLD))
    assert prune(root, current.name, keep='0').returncode == 0
    assert claimed.exists()


def test_a_bad_keep_value_warns_and_never_fails_setup(tmp_path):
    root = tmp_path / 'cache'
    current, old = entry(root, 1, time.time()), entry(root, 2, OLD)
    result = prune(root, current.name, keep='ten')
    assert result.returncode == 0 and 'not pruning' in result.stderr and old.exists()


def test_check_backs_off_from_a_folder_being_evicted(tmp_path):
    root = tmp_path / 'cache'
    folder = entry(root, 1, time.time())
    (folder / '.evicting').mkdir()
    env = {'PATH': os.environ['PATH'], 'FRI_OFFLINE_CACHE': str(root), 'GENE_SHA': folder.name}
    result = subprocess.run(['bash', str(SCRIPT), 'check'], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and 'being pruned' in result.stderr
    assert not list(folder.glob('.in-use.*'))
