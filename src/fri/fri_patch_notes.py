"""Owner-only release patch notes, sent from the maintainer side after a release.

    docker exec -i fri-runtime-1 python -m fri.fri_patch_notes send 0.4.0 < notes.md
    python -m fri.fri_patch_notes preview 0.4.0 < notes.md

The only recipient is GENE_OWNER_ID; nothing names anyone else. It logs in over REST only, with no
gateway session, so the running Fri is untouched. Each version reaches the owner once: every part
has a fixed nonce, FRI_DATA/patch-notes.json lists the parts Discord confirmed, and a part whose
send was attempted but never confirmed is looked for in the DM, after the newest message from before
that attempt (kept under `anchors`), before it is sent again.
Exit codes: 0 sent (or already sent), 2 refused (fix the notes or the record), 1 Discord or the
network failed (a rerun is safe)."""

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

LIMIT = 2000  # Discord's message length, in UTF-16 units.
MAX_PARTS = 4
EARLIEST = 1767225600  # 2026-01-01 UTC. No real attempt is older; an older attempt time is damage.
VERSION = re.compile(r'(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,5})')
ID = re.compile(r'[1-9]\d{0,19}')  # A Discord snowflake.
HASH = re.compile(r'[0-9a-f]{64}')
TRIED = {'attempted_at', 'sha256'}  # A part's states: {} before its send, then TRIED, then SENT.
SENT = TRIED | {'message_id', 'channel_id'}
NO_MENTIONS = discord.AllowedMentions.none()


class NotesRefused(Exception):
    """Notes that shouldn't reach the owner as given."""


class SendFailed(Exception):
    """Discord answered a send with something that can't be recorded; the part stays tried."""


def length(text):
    """Discord's count, not Python's: UTF-16 units, so most emoji count 2."""
    return len(text.encode('utf-16-le')) // 2


def parts(text):
    """Packs whole paragraphs (split at blank lines) into as few messages as fit."""
    text = text.replace('\r\n', '\n').strip()
    if not text:
        raise NotesRefused('the notes are empty')
    chunks = ['']
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = paragraph.strip('\n')
        if length(paragraph) > LIMIT:
            raise NotesRefused(f'a paragraph is over {LIMIT} characters')
        joined = f'{chunks[-1]}\n\n{paragraph}' if chunks[-1] else paragraph
        if length(joined) <= LIMIT:
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


def text_hash(chunk):
    return hashlib.sha256(chunk.encode()).hexdigest()


def recorded(part):
    """One part as send() writes it: when it was tried and its text's hash, then the message Discord confirmed."""
    if not isinstance(part, dict) or set(part) not in (set(), TRIED, SENT):
        return False
    attempted = part.get('attempted_at', EARLIEST)
    patterns = {'sha256': HASH, 'message_id': ID, 'channel_id': ID}
    return (isinstance(attempted, (int, float)) and not isinstance(attempted, bool) and
            EARLIEST <= attempted <= time.time() + 86400 and  # A real past attempt: no NaN, no 0, no far future.
            all(isinstance(part[key], str) and pattern.fullmatch(part[key]) for key, pattern in patterns.items() if key in part))


def stage(part):
    """0 before its send, 1 tried, 2 confirmed."""
    return 2 if 'message_id' in part else 1 if part else 0


def entry_ok(entry):
    """A version's notes hash and parts: sends go in order, so sent parts come first, then at most one tried.
    send() first saves a version with its first part tried, so a saved version with nothing tried is damage."""
    states = entry.get('parts') if isinstance(entry, dict) else None
    if not (isinstance(states, list) and 1 <= len(states) <= MAX_PARTS and all(recorded(part) for part in states) and
            isinstance(entry.get('sha256'), str) and HASH.fullmatch(entry['sha256'])):
        return False
    stages = [stage(part) for part in states]
    return stages == sorted(stages, reverse=True) and stages.count(1) <= 1 and stages[0] > 0


def sent_ids(state):
    return [part['message_id'] for entry in state['versions'].values() for part in entry['parts'] if 'message_id' in part]


def unique(pairs):
    """A JSON object, refusing a repeated key: the last one would silently drop the others' receipts."""
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError('a key appears twice')
    return dict(pairs)


def load(path):
    """The whole record, checked: a damaged one is refused, never guessed past, since a guess can resend."""
    try:
        state = json.loads(path.read_text(), object_pairs_hook=unique)
    except FileNotFoundError:
        return {'versions': {}}
    except (OSError, ValueError, RecursionError):  # Unreadable, truncated, not UTF-8, or nested past Python's limit.
        state = None
    versions = state.get('versions') if isinstance(state, dict) else None
    if (not isinstance(versions, dict) or not all(entry_ok(entry) for entry in versions.values()) or
            len(set(sent_ids(state))) != len(sent_ids(state))):  # One message confirms one part.
        raise NotesRefused(f'{path.name} is not a patch-notes record')
    return state


async def anchor(dm, attempted_at):
    """Where recovery of this attempt starts reading: the DM's newest message id before it ('' for an
    empty DM), or None when Discord gave one the record couldn't trust, so recovery falls back to time."""
    async for message in dm.history(limit=1):
        after = str(message.id)
        return {'attempted_at': attempted_at, 'after': after} if ID.fullmatch(after) else None
    return {'attempted_at': attempted_at, 'after': ''}


def search_from(state, version, index, part):
    """The message after which an attempt's own message would be: its anchor, which is Discord's clock,
    so a host clock that was fast or was corrected since can't start the search past the part. Anchors
    sit beside `versions`, which older code ignores and keeps, and count only for the attempt they were
    taken for. Without one, a minute before the attempt (or before now, if the clock stepped back)."""
    anchors = state.get('anchors')
    mark = anchors.get(version, {}).get(str(index)) if isinstance(anchors, dict) and isinstance(anchors.get(version), dict) else None
    if (isinstance(mark, dict) and mark.get('attempted_at') == part['attempted_at'] and
            isinstance(mark.get('after'), str) and (mark['after'] == '' or ID.fullmatch(mark['after']))):
        return discord.Object(id=int(mark['after'])) if mark['after'] else None
    return dt.datetime.fromtimestamp(min(part['attempted_at'], time.time()) - 60, dt.timezone.utc)


def set_anchor(state, version, index, mark):
    """Keeps one anchor per tried part, and none once it's confirmed or Discord's id was untrustworthy."""
    if not isinstance(state.get('anchors'), dict):
        state['anchors'] = {}
    marks = state['anchors'].setdefault(version, {})
    if not isinstance(marks, dict):
        marks = state['anchors'][version] = {}
    if mark:
        marks[str(index)] = mark
    else:
        marks.pop(str(index), None)
    if not marks:
        del state['anchors'][version]


async def landed(dm, me, chunk, after, taken):
    """The message from an earlier attempt, if Discord took it before that attempt failed or stopped.
    Every message after `after` is read (the whole DM if it was empty). Only Fri's own messages with
    exactly this text count, and never one the record already gives a part: Discord keeps no nonce in
    history, so that is what tells identical parts and releases apart."""
    async for message in dm.history(limit=None, after=after, oldest_first=True):
        if message.author.id == me and message.content == chunk and str(message.id) not in taken:
            return message
    return None


async def send(version, text, *, token, owner_id, data, out=print):
    chunks = parts(text)
    digest = hashlib.sha256(json.dumps(chunks).encode()).hexdigest()  # The split, not just the text.
    hashes = [text_hash(chunk) for chunk in chunks]
    path = data / 'patch-notes.json'
    with locked(data):
        state = load(path)
        entry = state['versions'].get(version)
        if entry and (entry['sha256'] != digest or len(entry['parts']) != len(chunks)):
            raise NotesRefused(f'{version} was already sent with different notes; use a new version')
        if entry is None:
            entry = state['versions'][version] = {'sha256': digest, 'parts': [{} for _ in chunks]}
        if any(part.get('sha256', h) != h for part, h in zip(entry['parts'], hashes)):
            raise NotesRefused(f'{path.name} is not a patch-notes record')
        if all(part.get('message_id') for part in entry['parts']):
            out(f'{version}: all {len(chunks)} parts were already sent')
            return
        tried = next((part['sha256'] for part in entry['parts'] if stage(part) == 1), None)
        twins = sorted(other for other, e in state['versions'].items() if other != version
                       for part in e['parts'] if stage(part) == 1 and part['sha256'] == tried)
        if twins:
            raise NotesRefused(f'{version} and {", ".join(twins)} each have a part with this text that was tried but '
                               f'never confirmed, and the DM can\'t say whose a message is; check it, then fix {path.name}')
        client = discord.Client(intents=discord.Intents.none())
        try:
            await client.login(token)
            owner = await client.fetch_user(owner_id)
            dm = await owner.create_dm()
            for index, (chunk, part) in enumerate(zip(chunks, entry['parts']), 1):
                if part.get('message_id'):
                    out(f'part {index}/{len(chunks)}: already sent')
                    continue
                taken = set(sent_ids(state))
                message = 'attempted_at' in part and await landed(
                    dm, client.user.id, chunk, search_from(state, version, index, part), taken)
                if not message:
                    attempted_at = time.time()
                    set_anchor(state, version, index, await anchor(dm, attempted_at))
                    part.update(attempted_at=attempted_at, sha256=hashes[index - 1])
                    private_json(path, state)
                    message = await dm.send(chunk, nonce=nonce(version, digest, index),
                                            allowed_mentions=NO_MENTIONS, suppress_embeds=True)
                    if str(message.id) in taken:
                        raise NotesRefused(f'Discord answered part {index} with message {message.id}, which '
                                           f'{path.name} already gives another part')
                if not (ID.fullmatch(str(message.id)) and ID.fullmatch(str(dm.id))):
                    # Recording it would make load() refuse every later send; a rerun looks for it instead.
                    raise SendFailed(f'Discord answered part {index} with message id {message.id!r} in DM {dm.id!r}')
                part.update(message_id=str(message.id), channel_id=str(dm.id))
                set_anchor(state, version, index, None)
                private_json(path, state)
                out(f'part {index}/{len(chunks)}: sent as message {message.id} ({length(chunk)} chars)')
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
    try:
        try:
            text = sys.stdin.buffer.read().decode('utf-8')
        except UnicodeDecodeError:
            raise NotesRefused('the notes are not UTF-8') from None
        if args.command == 'preview':
            for index, chunk in enumerate(parts(text), 1):
                print(f'--- part {index} ({length(chunk)} chars)\n{chunk}')
            return 0
        token, owner = os.getenv('DISCORD_TOKEN', ''), os.getenv('GENE_OWNER_ID', '')
        if not token or not ID.fullmatch(owner):
            raise NotesRefused('DISCORD_TOKEN and GENE_OWNER_ID must be set')
        asyncio.run(send(args.version, text, token=token, owner_id=int(owner), data=Path(os.getenv('FRI_DATA', '/data'))))
    except NotesRefused as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:  # Discord, the network or the host failed mid-send, in whatever form.
        # The record is written before each send and after each confirmation, so a rerun is safe.
        print(f'failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
