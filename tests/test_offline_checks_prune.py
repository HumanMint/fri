"""offline-checks.sh cache pruning, run under bash against throwaway caches."""

import os
import re
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'offline-checks.sh'
OLD = time.time() - 10 * 86400  # Past the 7-day floor.


def functions():
    text = SCRIPT.read_text()
    return re.search(r'^prune\(\) {.*?^}\n', text, re.M | re.S).group(0)


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


def test_a_folder_used_within_the_week_stays_even_past_the_cap(tmp_path):
    root = tmp_path / 'cache'
    current = entry(root, 1, time.time())
    recent = entry(root, 2, time.time() - 3 * 86400)
    assert prune(root, current.name, keep='0').returncode == 0
    assert recent.exists()


def test_equal_times_still_respect_the_cap(tmp_path):
    root = tmp_path / 'cache'
    current = entry(root, 99, time.time())
    folders = [entry(root, n, OLD) for n in range(1, 12)]
    assert prune(root, current.name, keep='1').returncode == 0
    assert sum(f.exists() for f in folders) == 1


def test_a_bad_keep_value_warns_and_never_fails_setup(tmp_path):
    root = tmp_path / 'cache'
    current, old = entry(root, 1, time.time()), entry(root, 2, OLD)
    result = prune(root, current.name, keep='ten')
    assert result.returncode == 0 and 'not pruning' in result.stderr and old.exists()
