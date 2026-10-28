"""Tracked files describe roles, never the maintainer's own installation.

The repository is public. Write "the bot host", "an optional download relay" or "a Linux host",
not the hosting provider, the maintainer's machines, network or unrelated services, or a
workstation path. Synthetic example addresses (the documentation ranges, loopback, private
ranges, 100.64.0.0/24 in tests) and generic range requirements such as 100.64.0.0/10 are fine.
Names that installed systems already read stay as they are; see COMPATIBILITY_NAMES.

Owner-specific names that aren't public already (unrelated services, machine and account names)
never appear here, not even hashed: short names are easy to guess back from a hash. A maintainer
sets PUBLIC_METADATA_RULES to a private JSON file of {"kind": ["name", ...]} to check those too.
"""

import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'uv.lock', 'tests/test_public_metadata.py'}
# Existing file names and service labels that installations depend on.
COMPATIBILITY_NAMES = ('fri-hostinger-id.json', 'local.dan.gene.download-relay')
# The first compatibility name already carries the hosting provider's name, so checking for it
# here discloses nothing new.
PROVIDER = COMPATIBILITY_NAMES[0].split('-')[1]
RULES = 'PUBLIC_METADATA_RULES'
PATTERNS = {
    'hosting provider': re.compile(rf'(?i)\b{re.escape(PROVIDER)}\b'),
    'machine or account name': re.compile(r'(?i)\bmac ?mini\b|\bmacbook\b'),
    # "the Mac" or "the mac", but not the MAC acronym ("verify the MAC", "the MAC address").
    'owner topology': re.compile(r'\b[Tt]he [Mm]ac\b(?!\s+address)|(?i:\bMac (relay|audio|agents?|Tailscale)\b|\bMac-offline\b'
                                 r'|\bhome[- ](relay|network|connection|machine)\b)'),
    'workstation path': re.compile(r'/Users/\w|/private/tmp/|/var/folders/|/tmp/claude'),
    'launch-agent label': re.compile(r'\blocal\.dan\.'),
    # Starts only where a name can start, so a long run of word characters is scanned once.
    'tailnet name': re.compile(r'(?i)(?<![\w-])[\w-]+\.ts\.net\b'),
    'tailnet address': re.compile(r'(?i)\bfd7a:115c:a1e0:(?!:/48)'),
}
IPV4 = re.compile(r'(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.]*\d)')
TAILNET = ipaddress.ip_network('100.64.0.0/10')
SYNTHETIC_TAILNET = ipaddress.ip_network('100.64.0.0/24')


PRIVATE_LABEL = 'private name'  # The only label private matches get, so output never names a private category.


def private_patterns(environ=os.environ):
    """The maintainer's private names, from the file PUBLIC_METADATA_RULES names; none when it's unset.

    The file is an object of lists of non-empty names; keys starting with "_" are notes. Any problem
    with it fails with one fixed message that names neither the path nor what's in it."""
    path = environ.get(RULES)
    if not path:
        return []
    try:
        rules = json.loads(Path(path).read_text(encoding='utf-8'))
        lists = [names for key, names in rules.items() if not key.startswith('_')]
        if not all(isinstance(names, list) and names and all(isinstance(name, str) and name.strip() for name in names)
                   for names in lists):
            raise ValueError
    except Exception:
        raise RuntimeError(f'{RULES} must name a readable JSON object of lists of non-empty names') from None
    return [re.compile(r'(?i)(?<![\w-])(?:' + '|'.join(re.escape(name.strip()).replace(r'\ ', r'\s+') for name in names)
                       + r')(?![\w-])') for names in lists]


PRIVATE = private_patterns()


def findings(line, private=PRIVATE):
    for name in COMPATIBILITY_NAMES:
        line = line.replace(name, '')
    for kind, pattern in PATTERNS.items():
        if pattern.search(line):
            yield kind
    if any(pattern.search(line) for pattern in private):
        yield PRIVATE_LABEL
    for match in IPV4.finditer(line):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if address in TAILNET and address not in SYNTHETIC_TAILNET:
            yield 'tailnet address'
        elif address.is_global:
            yield 'public address'


def tracked_text(path):
    """What Git stores for a tracked file: a symlink's own target, or text (bad bytes replaced); None for binary."""
    if path.is_symlink():
        return os.readlink(path)
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None
    return None if b'\0' in data else data.decode('utf-8', errors='replace')


def tracked_files():
    try:
        listed = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip('not a git checkout')
    return [name for name in listed.stdout.decode().split('\0') if name and name not in SKIP]


def test_tracked_files_name_no_private_deployment_details():
    found = []
    for name in tracked_files():
        text = tracked_text(ROOT / name)
        for number, line in enumerate((text or '').splitlines(), 1):
            found += [f'{name}:{number}: {kind}' for kind in dict.fromkeys(findings(line))]
    assert not found, ('Use generic roles instead of private deployment details '
                       '(tests/test_public_metadata.py):\n' + '\n'.join(found))


@pytest.mark.parametrize('line', [
    "host_path = ROOT / '.secrets/fri-hostinger-id.json'",
    "LABEL = 'local.dan.gene.download-relay'",
    "relay_url='http://100.64.0.1:2335'",
    "not in ipaddress.ip_network('100.64.0.0/10')",
    'Bind 127.0.0.1:8787 or 0.0.0.0 inside the network; examples use 192.0.2.10 and 10.0.0.2.',
    'Tailscale peers use fd7a:115c:a1e0::/48 and 100.64.0.0/10.',
    'Run it on the bot host; an optional download relay transfers files.',
    "me = api('/users/@me')",
    '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
    'Lavalink 3.7.13+red.5 and yt-dlp 2026.8.19 are pinned.',
    'Reads the MAC address column, then the home directory.',
    'Verify the MAC before decrypting the packet.',
])
def test_generic_text_passes(line):
    assert not list(findings(line, []))


@pytest.mark.parametrize('line, kind', [
    ('Install it on ' + PROVIDER.title() + '.', 'hosting provider'),
    ('See /Users/someone/Projects/gene/docs.', 'workstation path'),
    ('Probe: /private/tmp/review/test_probe.py', 'workstation path'),
    ('Probe: /var/folders/xy/T/test_probe.py', 'workstation path'),
    ('The relay runs on the Mac mini.', 'machine or account name'),
    ('Downloads go through the home relay.', 'owner topology'),
    ('it runs on the mac relay', 'owner topology'),
    ('IT RUNS ON THE MAC RELAY', 'owner topology'),
    ('Stop the old local.dan.gene.audio agent.', 'launch-agent label'),
    ('Connect to relay.example-tailnet.ts.net.', 'tailnet name'),
    ('GENE_RELAY_PEER=100.101.102.103', 'tailnet address'),
    ('Peer fd7a:115c:a1e0:ab12::1 only.', 'tailnet address'),
    ('The server is at 8.8.4.4.', 'public address'),
])
def test_private_details_are_found(line, kind):
    assert kind in findings(line, [])


def test_private_names_come_only_from_the_maintainers_rules_file(tmp_path):
    rules = tmp_path / 'rules.json'
    rules.write_text(json.dumps({'_note': 'kept private', 'synthetic-category': ['toyservice', 'toy box']}))
    private = private_patterns({RULES: str(rules)})
    assert list(findings('it syncs to ToyService nightly', private)) == [PRIVATE_LABEL]
    assert list(findings('back up the toy  box first', private)) == [PRIVATE_LABEL]
    assert not list(findings('toyservices and mytoyservice are other words', private))
    assert private_patterns({}) == []


@pytest.mark.parametrize('content', [None, '{not json', '[]', '{"service": "toyservice"}', '{"service": [""]}',
                                     '{"service": []}', '{"service": ["ok", 3]}'])
def test_a_bad_rules_file_fails_without_naming_its_path_or_contents(tmp_path, content):
    rules = tmp_path / 'synthetic-private-account' / 'rules.json'
    if content is not None:
        rules.parent.mkdir()
        rules.write_text(content)
    with pytest.raises(RuntimeError) as failure:
        private_patterns({RULES: str(rules)})
    assert 'synthetic-private-account' not in str(failure.value) and 'toyservice' not in str(failure.value)
    assert failure.value.__cause__ is None and failure.value.__suppress_context__


def test_a_long_word_is_scanned_in_linear_time():
    started = time.perf_counter()
    for _ in range(32):
        list(findings('x' * 32768, []))
    assert time.perf_counter() - started < 2


def test_symlinks_are_checked_by_their_stored_target_and_bad_bytes_by_the_rest_of_the_line(tmp_path):
    link = tmp_path / 'data'
    link.symlink_to('/Users/synthetic/Projects/toyservice/data')  # Its target needn't exist.
    assert 'workstation path' in findings(tracked_text(link), [])
    notes = tmp_path / 'notes.md'
    notes.write_bytes(b'peer=100.101.102.103\n# caf\xe9\n')
    assert 'tailnet address' in findings(tracked_text(notes).splitlines()[0], [])
    blob = tmp_path / 'image.png'
    blob.write_bytes(b'\x89PNG\0\0/Users/synthetic')
    assert tracked_text(blob) is None
