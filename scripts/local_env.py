#!/usr/bin/env python3
"""A local trial of Fri: write, check and run with a git-ignored `.env.local`.

  python scripts/local_env.py init                  # .env.local from .env.example, mode 0600
  python scripts/local_env.py check [--controller]  # validate it; --controller also reads /fri/contract
  python scripts/local_env.py run                   # start Fri with it

Usually called through scripts/local.sh. Nothing here prints a token or reads the ChatGPT session file;
only `check --controller` and `run` use the network, and only toward the configured controller (and
Discord, for `run`).
"""

import argparse
import ipaddress
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / '.env.local'
EXAMPLE = ROOT / '.env.example'
GENE_GUIDE = 'https://github.com/HumanMint/gene#readme'

# The example's container paths, swapped for paths inside this checkout (all git-ignored). They are also
# the defaults when the file leaves them out.
LOCAL_DEFAULTS = {
    'GENE_CONTROLLER_URL': 'http://127.0.0.1:8787',
    'FRI_DATA': '.state/fri-data',
    'FRI_CHATGPT_AUTH': '.secrets/fri-chatgpt.json',  # Where scripts/connect_chatgpt.py saves the sign-in.
}
HEADER = ['# Local trial of Fri. Git-ignored: never commit it or paste its values anywhere.',
          '# Explanations: docs/quickstart.md. Check it with: scripts/local.sh check']
IDS = ('GENE_GUILD_ID', 'GENE_OWNER_ID', 'GENE_WORKSHOP_CHANNEL_ID')
REQUIRED = {
    'DISCORD_TOKEN': 'Fri\'s bot token: Discord Developer Portal > your application > Bot > Reset Token',
    'GENE_FRI_TOKEN': 'Fri\'s service credential: the same value as GENE_FRI_TOKEN in the Gene controller\'s env',
    'GENE_GUILD_ID': 'your test server\'s ID: right-click the server > Copy Server ID',
    'GENE_OWNER_ID': 'your own user ID (you are Fri\'s owner): right-click your name > Copy User ID',
    'GENE_WORKSHOP_CHANNEL_ID': 'the text channel Fri always answers in: right-click it > Copy Channel ID',
    'GENE_CONTROLLER_URL': 'the Gene controller\'s address, http://127.0.0.1:8787 for a local one',
}
OPTIONAL = {
    'FRI_PRESENCE': 'invisible (default) or online',
    'FRI_DATA': 'Fri\'s own data directory (health file, receipts, run lock)',
    'FRI_CHATGPT_AUTH': 'the ChatGPT sign-in file that scripts/connect_chatgpt.py writes',
    'FRI_TIMEZONE': 'IANA zone for reminders without one (default America/Los_Angeles)',
    'FRI_CHAT_CHANNEL_ID': 'a channel where every message counts as talking to Fri',
    'GENE_BOT_ID': 'Gene\'s bot user ID, so Fri can follow Gene\'s replies and errors',
    'GENE_PREFIX': 'Gene\'s Red command prefix, which Fri leaves to Gene (default !)',
    'FRI_APPLICATION_ID': 'setup only: your application ID, to print an invite link',
    'FRI_RELEASE_SHA': 'release identity, set by the release coordinator; leave unset locally',
}
SNOWFLAKE = re.compile(r'[1-9][0-9]{16,19}')
TOKEN_PART = re.compile(r'[A-Za-z0-9_-]+')
# View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files, Read Message History,
# Add Reactions. No Administrator, Manage Server or voice permission.
PERMISSIONS = 1024 + 2048 + 274877906944 + 16384 + 32768 + 65536 + 64


def read_env(path):
    """KEY=VALUE lines, as Docker's env_file reads them; blank lines and # comments are skipped."""
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return values


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(text)


def setting(line):
    """The key a KEY=VALUE line sets, or None for comments and blank lines."""
    line = line.strip()
    return line.split('=', 1)[0].strip() if line and not line.startswith('#') and '=' in line else None


def init(env_file=ENV_FILE, example=EXAMPLE):
    """Write the local env file from the example, with local paths. An existing file keeps every value it has
    (pasted from Gene's `scripts/dev.py fri-env`, say); only missing settings are added."""
    template = [f'{key}={LOCAL_DEFAULTS[key]}' if (key := setting(line)) in LOCAL_DEFAULTS else line
                for line in example.read_text().splitlines()]
    if not env_file.exists():
        write_private(env_file, '\n'.join(HEADER + template) + '\n')
        print(f'Wrote {env_file.name} (mode 0600). Fill in the empty values, then run scripts/local.sh check.')
        return 0
    text = env_file.read_text()
    present = set(read_env(env_file))
    missing = [line for line in template if setting(line) and setting(line) not in present]
    if not missing:
        print(f'{env_file.name} already has every setting; leaving it as it is.')
        return 0
    with env_file.open('a') as output:
        output.write(('\n' if text and not text.endswith('\n') else '') + '\n'.join(missing) + '\n')
    print(f'Added {", ".join(setting(line) for line in missing)} to {env_file.name}; its existing values were kept.')
    return 0


def local_path(value, root=ROOT):
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def private_host(host):
    if host in ('localhost', 'controller'):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def findings(values, root=ROOT, env_mode=None):
    """(level, key, message) for every problem, never echoing a value. Levels: error, warn, ok."""
    found = []

    def add(level, key, message):
        found.append((level, key, message))

    for key, hint in REQUIRED.items():
        value = values.get(key, '')
        if not value:
            add('error', key, 'missing: ' + hint)
        elif len(value) > 1 and value[0] == value[-1] and value[0] in '"\'':
            add('error', key, 'remove the quotes around the value')
        elif any(character.isspace() for character in value):
            add('error', key, 'contains whitespace')
    errors = {key for level, key, _ in found if level == 'error'}

    token = values.get('DISCORD_TOKEN', '')
    if 'DISCORD_TOKEN' not in errors:
        parts = token.split('.')
        if token.lower().startswith('bot'):
            add('error', 'DISCORD_TOKEN', 'paste the token alone, without a "Bot " prefix')
        elif len(token) < 50 or not all(TOKEN_PART.fullmatch(part) for part in parts):
            add('error', 'DISCORD_TOKEN', 'does not look like a bot token; copy it again from Bot > Reset Token')
        elif len(parts) != 3:
            add('warn', 'DISCORD_TOKEN', 'set (hidden), but not the usual three dot-separated parts')
        else:
            add('ok', 'DISCORD_TOKEN', 'set (hidden)')
    service = values.get('GENE_FRI_TOKEN', '')
    if 'GENE_FRI_TOKEN' not in errors:
        if len(service) < 32:
            add('error', 'GENE_FRI_TOKEN', 'the controller needs at least 32 characters')
        elif service == token:
            add('error', 'GENE_FRI_TOKEN', 'must differ from the Discord token')
        else:
            add('ok', 'GENE_FRI_TOKEN', 'set (hidden); must equal the controller\'s GENE_FRI_TOKEN')
    for key in IDS:
        if key not in errors:
            if SNOWFLAKE.fullmatch(values[key]):
                add('ok', key, 'a Discord ID; must match the controller\'s ' + key)
            else:
                add('error', key, 'not a Discord ID (17 to 20 digits): ' + REQUIRED[key])
    if 'GENE_CONTROLLER_URL' not in errors:
        try:
            parts = urlsplit(values['GENE_CONTROLLER_URL'])
        except ValueError:  # An unbalanced IPv6 bracket, for example.
            parts = None
        try:
            port = parts and parts.port  # Raises unless the port is a number from 0 to 65535.
        except ValueError:
            port = 0
        if parts is None or parts.scheme not in ('http', 'https') or not parts.hostname:
            add('error', 'GENE_CONTROLLER_URL', 'use a URL like http://127.0.0.1:8787')
        elif port == 0:
            add('error', 'GENE_CONTROLLER_URL', 'the port must be a number from 1 to 65535')
        elif parts.hostname == 'controller':
            add('warn', 'GENE_CONTROLLER_URL', '"controller" only resolves inside Docker; '
                'a controller on this machine is http://127.0.0.1:8787')
        elif not private_host(parts.hostname):
            add('warn', 'GENE_CONTROLLER_URL', 'not a local or private address; never expose the controller publicly')
        else:
            add('ok', 'GENE_CONTROLLER_URL', 'a local address')

    for key in ('FRI_CHAT_CHANNEL_ID', 'GENE_BOT_ID', 'FRI_APPLICATION_ID'):
        if values.get(key) and not SNOWFLAKE.fullmatch(values[key]):
            add('error', key, 'not a Discord ID (17 to 20 digits)')
    if values.get('FRI_PRESENCE', 'invisible') not in ('', 'invisible', 'online'):
        add('warn', 'FRI_PRESENCE', 'only "online" changes anything; other values mean invisible')
    if values.get('FRI_TIMEZONE'):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(values['FRI_TIMEZONE'])
        except (ZoneInfoNotFoundError, ValueError):
            add('error', 'FRI_TIMEZONE', 'not an IANA time zone such as Europe/Berlin')
    if values.get('FRI_RELEASE_SHA'):
        add('warn', 'FRI_RELEASE_SHA', 'leave it unset locally; health then reports "development"')

    data = local_path(values.get('FRI_DATA') or LOCAL_DEFAULTS['FRI_DATA'], root)
    existing = next((path for path in (data, *data.parents) if path.exists()), None)
    if existing is None or not existing.is_dir():
        add('error', 'FRI_DATA', 'neither the directory nor a parent of it exists')
    elif not os.access(existing, os.W_OK | os.X_OK):
        add('error', 'FRI_DATA', f'{existing} is not writable')
    else:
        add('ok', 'FRI_DATA', 'writable' if data.exists() else 'will be created on start')

    session = local_path(values.get('FRI_CHATGPT_AUTH') or LOCAL_DEFAULTS['FRI_CHATGPT_AUTH'], root)
    # Only the file's type and mode are read, never its contents.
    if not session.exists():
        add('warn', 'FRI_CHATGPT_AUTH', 'no ChatGPT sign-in yet: Fri connects, but every model reply fails and '
            'the owner gets one DM about it. Connect with: uv run --no-sync python scripts/connect_chatgpt.py')
    elif not session.is_file():
        add('error', 'FRI_CHATGPT_AUTH', 'not a file')
    elif not os.access(session.parent, os.W_OK | os.X_OK):
        add('error', 'FRI_CHATGPT_AUTH', 'its directory must be writable: renewals replace the file there')
    elif session.stat().st_mode & 0o077:
        add('warn', 'FRI_CHATGPT_AUTH', 'readable by other users; chmod 600 it')
    else:
        add('ok', 'FRI_CHATGPT_AUTH', 'present (contents not read)')

    for key in sorted(set(values) - set(REQUIRED) - set(OPTIONAL)):
        add('warn', key, 'not a setting Fri reads; check the name')
    if env_mode is not None and env_mode & 0o077:
        add('warn', ENV_FILE.name, 'readable by other users; chmod 600 it')
    return found


def address(url):
    """The URL's scheme, host and port, for messages: never a user name, password, path or query."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or '?', parts.port
    except ValueError:
        return 'GENE_CONTROLLER_URL'
    host = f'[{host}]' if ':' in host else host
    return f'{parts.scheme}://{host}' + (f':{port}' if port else '')


def probe(url, token, transport=None):
    """Read the controller's contract as Fri's startup does. Returns (level, message) lines."""
    import httpx

    from fri.fri_protocol import CAPABILITIES, PROTOCOL, compatible
    from fri.fri_protocol import OPTIONAL as OPTIONAL_CAPABILITIES
    url = url.rstrip('/')
    try:
        with httpx.Client(timeout=5, trust_env=False, transport=transport) as client:
            response = client.get(url + '/fri/contract',
                                  headers={'Authorization': 'Bearer ' + token, 'X-Fri-Contract': PROTOCOL})
    except httpx.HTTPError as exc:
        return [('error', f'no controller answered at {address(url)} ({type(exc).__name__}). Start it first: {GENE_GUIDE}')]
    if response.status_code == 401:
        return [('error', 'the controller refused GENE_FRI_TOKEN: it must equal the controller\'s GENE_FRI_TOKEN')]
    if response.status_code != 200:
        return [('error', f'the controller answered HTTP {response.status_code} to /fri/contract')]
    try:
        contract = response.json()
    except ValueError:
        contract = None
    if not compatible(contract):
        offered = contract.get('capabilities') if isinstance(contract, dict) else None
        missing = [item for item in CAPABILITIES if not isinstance(offered, list) or item not in offered]
        return [('error', f'the controller does not speak {PROTOCOL} with every required capability '
                 f'(missing: {", ".join(missing) or "protocol"}). Use the Gene commit in docs/integration-pair.json '
                 'or newer.')]
    lines = [('ok', f'controller contract {PROTOCOL}, every required capability offered')]
    absent = [item for item in OPTIONAL_CAPABILITIES if item not in contract['capabilities']]
    if absent:
        lines.append(('warn', 'optional capabilities not offered (those features stay off): ' + ', '.join(absent)))
    return lines


def report(found):
    for level, key, message in found:
        print(f'{level:<6} {key:<25} {message}')


def check(env_file=ENV_FILE, controller=False, transport=None, hint=True):
    if not env_file.exists():
        print(f'No {env_file.name} yet. Run: scripts/local.sh setup')
        return 1
    values = read_env(env_file)
    found = findings(values, env_mode=env_file.stat().st_mode & 0o777)
    report(found)
    failed = any(level == 'error' for level, _, _ in found)
    if any(level == 'error' and key in IDS for level, key, _ in found):
        print('tip    Copy ID appears once Developer Mode is on: User Settings > Advanced > Developer Mode')
    if controller:
        if {'GENE_FRI_TOKEN', 'GENE_CONTROLLER_URL'} & {key for level, key, _ in found if level == 'error'}:
            print('skip   controller                fix GENE_FRI_TOKEN and GENE_CONTROLLER_URL first')
        else:
            lines = probe(values['GENE_CONTROLLER_URL'], values['GENE_FRI_TOKEN'], transport)
            report([(level, 'controller', message) for level, message in lines])
            failed = failed or any(level == 'error' for level, _ in lines)
    if failed:
        print(f'\n{env_file.name} needs changes before Fri can start. Explanations: docs/quickstart.md')
        return 1
    application = values.get('FRI_APPLICATION_ID')
    if application:
        print('\nInvite Fri to your test server (bot and slash commands, only the permissions Fri uses):')
        print(f'https://discord.com/oauth2/authorize?client_id={application}&scope=bot%20applications.commands'
              f'&permissions={PERMISSIONS}&guild_id={values["GENE_GUILD_ID"]}&disable_guild_select=true')
    if hint:
        print(f'\n{env_file.name} looks ready. Start Fri with: scripts/local.sh run')
    return 0


def run(env_file=ENV_FILE):
    if check(env_file, hint=False) != 0:
        return 1
    values = read_env(env_file)
    environment = {**os.environ, **values}
    for key in ('FRI_DATA', 'FRI_CHATGPT_AUTH'):
        environment[key] = str(local_path(values.get(key) or LOCAL_DEFAULTS[key]))
    Path(environment['FRI_DATA']).mkdir(parents=True, exist_ok=True, mode=0o700)
    print('\nStarting Fri (Ctrl-C stops them).\n', flush=True)
    os.execve(sys.executable, [sys.executable, '-m', 'fri.fri'], environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='write .env.local from .env.example, or add its missing settings')
    verify = commands.add_parser('check', help='validate .env.local without printing values')
    verify.add_argument('--controller', action='store_true', help='also read the controller\'s /fri/contract')
    commands.add_parser('run', help='start Fri with .env.local')
    args = parser.parse_args(argv)
    if args.command == 'init':
        return init()
    if args.command == 'check':
        return check(controller=args.controller)
    return run()


if __name__ == '__main__':
    sys.exit(main())
