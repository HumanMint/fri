"""Owner-only release patch notes, sent from the maintainer side after a release.

    docker exec -i fri-runtime-1 python -m fri.fri_patch_notes send 0.4.0 < notes.md
    python -m fri.fri_patch_notes preview 0.4.0 < notes.md

The only recipient is GENE_OWNER_ID; nothing names anyone else. It logs in over REST only, with no
gateway session, so the running Fri is untouched. Each version reaches the owner once: every part
has a fixed nonce, FRI_DATA/patch-notes.json lists the parts Discord confirmed, and a part whose
send was attempted but never confirmed is looked for in the DM before it is sent again."""

import argparse
import asyncio
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import discord

from .private_files import private_json

LIMIT = 2000  # Discord's message length.
MAX_PARTS = 4
VERSION = re.compile(r'(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,5})')
NO_MENTIONS = discord.AllowedMentions.none()


class NotesRefused(Exception):
    """Notes that shouldn't reach the owner as given."""


def parts(text):
    """Packs whole paragraphs (split at blank lines) into as few messages as fit."""
    text = text.replace('\r\n', '\n').strip()
    if not text:
        raise NotesRefused('the notes are empty')
    chunks = ['']
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = paragraph.strip('\n')
        if len(paragraph) > LIMIT:
            raise NotesRefused(f'a paragraph is over {LIMIT} characters')
        joined = f'{chunks[-1]}\n\n{paragraph}' if chunks[-1] else paragraph
        if len(joined) <= LIMIT:
            chunks[-1] = joined
        else:
            chunks.append(paragraph)
    if len(chunks) > MAX_PARTS:
        raise NotesRefused(f'the notes need {len(chunks)} messages; at most {MAX_PARTS} are sent')
    return chunks


def nonce(version, digest, index):
    seed = f'patch-notes/{version}/{digest}/{index}'.encode()
    return int.from_bytes(hashlib.sha256(seed).digest()[:7], 'big')


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / 'patch-notes.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NotesRefused('another patch-notes send is running') from None
        yield
    finally:
        os.close(fd)


def recorded(part):
    """One part as send() writes it: when it was attempted, and the message Discord confirmed."""
    attempted = part.get('attempted_at', 0)
    return (isinstance(part, dict) and set(part) <= {'attempted_at', 'message_id', 'channel_id'} and
            isinstance(attempted, (int, float)) and not isinstance(attempted, bool) and
            0 <= attempted <= time.time() + 86400 and  # A real past attempt: no NaN, no far future.
            all(isinstance(part.get(key, '0'), str) and part.get(key, '0').isdigit() for key in ('message_id', 'channel_id')))


def load(path):
    """The whole record, checked: a damaged one is refused, never guessed past, since a guess can resend."""
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {'versions': {}}
    except (OSError, ValueError):  # Unreadable, truncated or not UTF-8 JSON.
        state = None
    versions = state.get('versions') if isinstance(state, dict) else None
    if not isinstance(versions, dict) or not all(
            isinstance(entry, dict) and isinstance(entry.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
            and isinstance(entry.get('parts'), list) and 1 <= len(entry['parts']) <= MAX_PARTS
            and all(isinstance(part, dict) and recorded(part) for part in entry['parts'])
            for entry in versions.values()):
        raise NotesRefused(f'{path.name} is not a patch-notes record')
    return state


async def landed(dm, me, chunk, attempted_at):
    """The message from an earlier attempt, if Discord took it before that attempt failed or stopped.
    Clocks get a minute either side; only Fri's own messages with exactly this text count."""
    after = dt.datetime.fromtimestamp(attempted_at - 60, dt.timezone.utc)
    async for message in dm.history(limit=50, after=after, oldest_first=True):
        if message.author.id == me and message.content == chunk:
            return message
    return None


async def send(version, text, *, token, owner_id, data, out=print):
    chunks = parts(text)
    digest = hashlib.sha256(json.dumps(chunks).encode()).hexdigest()  # The split, not just the text.
    path = data / 'patch-notes.json'
    with locked(data):
        state = load(path)
        entry = state['versions'].get(version)
        if entry and (entry.get('sha256') != digest or len(entry.get('parts', ())) != len(chunks)):
            raise NotesRefused(f'{version} was already sent with different notes; use a new version')
        if entry is None:
            entry = state['versions'][version] = {'sha256': digest, 'parts': [{} for _ in chunks]}
        if all(part.get('message_id') for part in entry['parts']):
            out(f'{version}: all {len(chunks)} parts were already sent')
            return
        client = discord.Client(intents=discord.Intents.none())
        await client.login(token)
        try:
            owner = await client.fetch_user(owner_id)
            dm = await owner.create_dm()
            for index, (chunk, part) in enumerate(zip(chunks, entry['parts']), 1):
                if part.get('message_id'):
                    out(f'part {index}/{len(chunks)}: already sent')
                    continue
                message = part.get('attempted_at') and await landed(dm, client.user.id, chunk, part['attempted_at'])
                if not message:
                    part['attempted_at'] = time.time()
                    private_json(path, state)
                    message = await dm.send(chunk, nonce=nonce(version, digest, index),
                                            allowed_mentions=NO_MENTIONS, suppress_embeds=True)
                part.update(message_id=str(message.id), channel_id=str(dm.id))
                private_json(path, state)
                out(f'part {index}/{len(chunks)}: sent as message {message.id} ({len(chunk)} chars)')
        finally:
            await client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m fri.fri_patch_notes',
                                     description='DM the owner one release\'s patch notes, read from stdin.')
    parser.add_argument('command', choices=('send', 'preview'))
    parser.add_argument('version', help='the release version, like 0.4.0')
    args = parser.parse_args(argv)
    if not VERSION.fullmatch(args.version):
        parser.error('version must look like 0.4.0')
    text = sys.stdin.buffer.read().decode('utf-8')
    try:
        if args.command == 'preview':
            for index, chunk in enumerate(parts(text), 1):
                print(f'--- part {index} ({len(chunk)} chars)\n{chunk}')
            return 0
        asyncio.run(send(args.version, text, token=os.environ['DISCORD_TOKEN'],
                         owner_id=int(os.environ['GENE_OWNER_ID']),
                         data=Path(os.getenv('FRI_DATA', '/data'))))
    except NotesRefused as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
