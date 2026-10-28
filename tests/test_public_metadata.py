"""Tracked files describe roles, never the maintainer's own installation.

The repository is public. Write "the bot host", "an optional download relay" or "a Linux host",
not the hosting provider, the maintainer's machines, network or unrelated services, or a
workstation path. Synthetic example addresses (the documentation ranges, loopback, private
ranges, 100.64.0.0/24 in tests) and generic range requirements such as 100.64.0.0/10 are fine.
Names that installed systems already read stay as they are; see COMPATIBILITY_NAMES.
"""

import hashlib
import ipaddress
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'uv.lock', 'tests/test_public_metadata.py'}
# Existing file names and service labels that installations depend on.
COMPATIBILITY_NAMES = ('fri-hostinger-id.json', 'local.dan.gene.download-relay')
# Private names are stored hashed so this public file doesn't list them: the hosting provider,
# unrelated personal services, and machine or account names (sha256 of the lowercase word or
# two-word phrase, first 16 hex digits).
HASHED = {
    'fefaa42da40ecf93': 'hosting provider',
    '96a4bc2602655473': 'unrelated personal service',
    'a2bdc78162b620e9': 'unrelated personal service',
    '1edc9fedfb83ca51': 'unrelated personal service',
    '19ea087c7722c524': 'unrelated personal service',
    '82a6e38dbc9139a4': 'unrelated personal service',
    '8cc5f23e2d030338': 'machine or account name',
}
PATTERNS = {
    'machine or account name': re.compile(r'(?i)\bmac ?mini\b|\bmacbook\b|\bdan-mac'),
    'owner topology': re.compile(r'\bthe Mac\b|\bMac (relay|audio|agents?|Tailscale)\b|\bMac-offline\b'
                                 r'|(?i:\bhome[- ](relay|network|connection|machine)\b)'),
    'workstation path': re.compile(r'/Users/\w|/private/tmp/|/var/folders/|/tmp/claude'),
    'launch-agent label': re.compile(r'\blocal\.dan\.'),
    'tailnet name': re.compile(r'(?i)[\w-]+\.ts\.net\b'),
    'tailnet address': re.compile(r'(?i)\bfd7a:115c:a1e0:(?!:/48)'),
}
WORD = re.compile(r'[a-z0-9]+')
IPV4 = re.compile(r'(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.]*\d)')
TAILNET = ipaddress.ip_network('100.64.0.0/10')
SYNTHETIC_TAILNET = ipaddress.ip_network('100.64.0.0/24')


def digest(phrase):
    return hashlib.sha256(phrase.encode()).hexdigest()[:16]


def findings(line):
    for name in COMPATIBILITY_NAMES:
        line = line.replace(name, '')
    words = WORD.findall(line.lower())
    for phrase in words + [' '.join(pair) for pair in zip(words, words[1:])]:
        if (kind := HASHED.get(digest(phrase))):
            yield kind
    for kind, pattern in PATTERNS.items():
        if pattern.search(line):
            yield kind
    for match in IPV4.finditer(line):
        try:
            address = ipaddress.ip_address(match.group())
        except ValueError:
            continue
        if address in TAILNET and address not in SYNTHETIC_TAILNET:
            yield 'tailnet address'
        elif address.is_global:
            yield 'public address'


def tracked_files():
    try:
        listed = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip('not a git checkout')
    return [name for name in listed.stdout.decode().split('\0') if name and name not in SKIP]


def test_tracked_files_name_no_private_deployment_details():
    found = []
    for name in tracked_files():
        try:
            text = (ROOT / name).read_text(encoding='utf-8')
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
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
])
def test_generic_text_passes(line):
    assert not list(findings(line))


@pytest.mark.parametrize('line, kind', [
    ('Install it on ' + COMPATIBILITY_NAMES[0].split('-')[1].title() + '.', 'hosting provider'),
    ('See /Users/someone/Projects/gene/docs.', 'workstation path'),
    ('Probe: /private/tmp/review/test_probe.py', 'workstation path'),
    ('Probe: /var/folders/xy/T/test_probe.py', 'workstation path'),
    ('The relay runs on the Mac mini.', 'machine or account name'),
    ('Downloads go through the home relay.', 'owner topology'),
    ('Stop the old local.dan.gene.audio agent.', 'launch-agent label'),
    ('Connect to relay.example-tailnet.ts.net.', 'tailnet name'),
    ('GENE_RELAY_PEER=100.101.102.103', 'tailnet address'),
    ('Peer fd7a:115c:a1e0:ab12::1 only.', 'tailnet address'),
    ('The server is at 8.8.4.4.', 'public address'),
])
def test_private_details_are_found(line, kind):
    assert kind in findings(line)
