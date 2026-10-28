"""Release patch notes, drafted by Fri and sent from the maintainer side after a release.

    docker exec -i fri-runtime-1 python -m fri.fri_patch_notes draft 0.7.0 < facts.md > notes.md
    python -m fri.fri_patch_notes preview 0.7.0 < notes.md
    docker exec -i fri-runtime-1 python -m fri.fri_patch_notes send 0.7.0 --channel fri-chat < notes.md   # + changelog
    docker exec -i fri-runtime-1 python -m fri.fri_patch_notes pin 0.7.0 --channel fri-chat   # the pin alone
    docker exec -i fri-runtime-1 python -m fri.fri_patch_notes send 0.7.0 < note.md   # the owner's DM

`draft` has Fri's own model write the post from the maintainer's facts, in Fri's voice, to the server.
`send` posts to one named text channel of GENE_GUILD_ID, or with no --channel DMs GENE_OWNER_ID; nothing
names anyone else. It logs in over REST only, with no gateway session, so the running Fri is untouched.
Each version reaches each place once: every part has a fixed nonce, FRI_DATA/patch-notes.json (the DM)
or patch-notes-server.json (the channel) lists the parts Discord confirmed, and a part whose send was
attempted but never confirmed is looked for there, after the newest message from before that attempt
(kept under `anchors`), before it is sent again.
A channel post is one message: a short summary, with the full changelog attached as fri-<version>-changelog.md
(the notes file holds the summary, a `<!-- changelog -->` line, then the changelog). It is then pinned there. The
record keeps the pinned message's id, so a rerun after a failed pin only pins, never posts again, and
`pin` pins a version already posted from the record alone. Fri never unpins or removes anything.
The running Fri only reads both records (Fri.posted_notes), so a reply to a posted part sees its words.
Exit codes: 0 sent (or already sent) and, in a channel, pinned; 2 refused (fix the notes or the record);
1 Discord or the network failed (a rerun is safe); 3 posted but not pinned (the post stands; a rerun
only tries the pin)."""

import argparse
import asyncio
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import discord

from .fri_agent_model import STRING, obj
from .private_files import private_json

LIMIT = 2000  # Discord's message length, in UTF-16 units.
MAX_PARTS = 4
CHANGELOG_MARK = '<!-- changelog -->'  # A line of its own: the summary above it, the attached changelog below.
CHANGELOG_LIMIT = 512 * 1024  # bytes; far under Discord's attachment limit
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


class PinFailed(Exception):
    """The notes are posted, but Discord didn't pin them; a rerun only tries the pin again."""


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


def split_changelog(text):
    """The post's text and the full changelog attached to it, split at a `<!-- changelog -->` line (None without one)."""
    lines = text.replace('\r\n', '\n').split('\n')
    marks = [i for i, line in enumerate(lines) if line.strip() == CHANGELOG_MARK]
    if not marks:
        return text, None
    if len(marks) > 1:
        raise NotesRefused(f'the notes have {len(marks)} {CHANGELOG_MARK} lines; need one')
    changelog = '\n'.join(lines[marks[0]+1:]).strip()
    if not changelog:
        raise NotesRefused('the changelog after the mark is empty')
    if len(changelog.encode()) > CHANGELOG_LIMIT:
        raise NotesRefused(f'the changelog is over {CHANGELOG_LIMIT // 1024} KiB')
    return '\n'.join(lines[:marks[0]]), changelog + '\n'


def changelog_name(version):
    return f'fri-{version}-changelog.md'


def nonce(version, digest, index, place=''):
    """Fixed per part and place: Discord dedupes nonces per author, not per channel, so a channel's are its own."""
    seed = f'patch-notes/{version}/{digest}/{index}{place}'.encode()
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
    pinned = 'pinned' not in entry or (set(stages) == {2} and entry['pinned'] == states[0]['message_id'])
    trimmed = 'trimmed' not in entry or (isinstance(entry['trimmed'], list) and len(entry['trimmed']) == len(states) and
                                         all(isinstance(h, str) and HASH.fullmatch(h) for h in entry['trimmed']))
    changelog = 'changelog' not in entry or (isinstance(entry['changelog'], str) and HASH.fullmatch(entry['changelog'])
                                             and len(states) == 1)
    return (stages == sorted(stages, reverse=True) and stages.count(1) <= 1 and stages[0] > 0 and pinned and trimmed
            and changelog)


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


def snowflake(text):
    """An id Discord could have made: a read after one past its unsigned 64 bits is refused outright."""
    return ID.fullmatch(text) is not None and int(text) < 2**64


async def anchor(dm, attempted_at):
    """Where recovery of this attempt starts reading: the DM's newest message id before it ('' for an
    empty DM), or None when Discord gave one the record couldn't trust, so recovery falls back to time."""
    async for message in dm.history(limit=1):
        after = str(message.id)
        return {'attempted_at': attempted_at, 'after': after} if snowflake(after) else None
    return {'attempted_at': attempted_at, 'after': ''}


def search_from(state, version, index, part):
    """The message after which an attempt's own message would be: its anchor, which is Discord's clock,
    so a host clock that was fast or was corrected since can't start the search past the part. Anchors
    sit beside `versions`, which older code ignores and keeps, and count only for the attempt they were
    taken for. Without one, a minute before the attempt (or before now, if the clock stepped back)."""
    anchors = state.get('anchors')
    mark = anchors.get(version, {}).get(str(index)) if isinstance(anchors, dict) and isinstance(anchors.get(version), dict) else None
    if (isinstance(mark, dict) and mark.get('attempted_at') == part['attempted_at'] and
            isinstance(mark.get('after'), str) and (mark['after'] == '' or snowflake(mark['after']))):
        return discord.Object(id=int(mark['after'])) if mark['after'] else None
    return dt.datetime.fromtimestamp(min(part['attempted_at'], time.time()) - 60, dt.timezone.utc)


def kept(start):
    """A retry's anchor from search_from's boundary: the earlier send may still land behind a new one, so
    the boundary stays put. A time one, from code before anchors, becomes the id Discord reads after for it."""
    if isinstance(start, dt.datetime):
        return str(discord.utils.time_snowflake(start, high=True))
    return str(start.id) if start else ''


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


async def landed(dm, me, chunk, after, taken, attachment=None):
    """The message from an earlier attempt, if Discord took it before that attempt failed or stopped.
    Every message after `after` is read (the whole DM if it was empty). Only Fri's own messages with
    exactly this text count (Discord trims the ends it stores), and never one the record already gives a
    part: Discord keeps no nonce in history, so that is what tells identical parts and releases apart."""
    async for message in dm.history(limit=None, after=after, oldest_first=True):
        if message.author.id == me and message.content.strip() == chunk.strip() and str(message.id) not in taken and (
                attachment is None or any(getattr(a, 'filename', None) == attachment for a in getattr(message, 'attachments', []))):
            return message
    return None


async def destination(client, *, owner_id, guild_id, channel):
    """The owner's DM, or the one text channel of the guild with this name: two with the name is a refusal."""
    if channel is None:
        owner = await client.fetch_user(owner_id)
        return await owner.create_dm()
    guild = await client.fetch_guild(guild_id)
    found = [c for c in await guild.fetch_channels() if c.type == discord.ChannelType.text and c.name == channel]
    if len(found) != 1:
        raise NotesRefused(f'the server has {len(found)} text channels named #{channel}; need exactly one')
    return found[0]


async def pin(room, entry, version, out):
    """Pin a channel post's first part, the one members find in the pins. Nothing in the channel is ever
    unpinned, removed or reposted, so a permission or pin-limit refusal leaves the post as it is."""
    message_id, channel_id = entry['parts'][0]['message_id'], entry['parts'][0]['channel_id']
    where = f'#{getattr(room, "name", room.id)}'
    if str(room.id) != channel_id:
        raise PinFailed(f'{version} was posted in channel {channel_id}, not {where} ({room.id}); nothing was pinned')
    try:
        message = await room.fetch_message(int(message_id))  # Even when on record: someone may have unpinned it.
        if not message.pinned:
            try:
                await message.pin(reason=f'Fri patch notes {version}')
            except discord.HTTPException:
                if not (await room.fetch_message(int(message_id))).pinned:  # A pin whose answer was lost still counts.
                    raise
    except discord.NotFound:
        raise PinFailed(f'message {message_id} is no longer in {where}; it was not posted again') from None
    except discord.Forbidden as exc:
        raise PinFailed(f'Fri may not pin in {where}; it needs Pin Messages and Read Message History ({exc})') from None
    except discord.HTTPException as exc:
        if exc.code == 30003:
            raise PinFailed(f'{where} has Discord\'s maximum of 250 pins; none was removed to make room') from None
        raise PinFailed(f'Discord failed the pin ({exc})') from None
    except Exception as exc:  # The network, or a reply Discord shouldn't send.
        raise PinFailed(f'{type(exc).__name__}: {exc}') from None
    entry['pinned'] = message_id
    out(f'{version}: message {message_id} is pinned in {where}')


async def pin_posted(version, *, token, data, guild_id, channel, out=print):
    """Pin a version already posted in the channel, from the record alone: nothing is sent."""
    path = data / 'patch-notes-server.json'
    with locked(data):
        state = load(path)
        entry = state['versions'].get(version)
        if not entry or not all(part.get('message_id') for part in entry['parts']):
            raise NotesRefused(f'{version} is not fully posted in a channel; send it first')
        client = discord.Client(intents=discord.Intents.none())
        try:
            await client.login(token)
            try:
                room = await destination(client, owner_id=None, guild_id=guild_id, channel=channel)
            except Exception as exc:
                raise PinFailed(f'#{channel} could not be read ({type(exc).__name__}: {exc})') from None
            await pin(room, entry, version, out)
            private_json(path, state)
        finally:
            await client.close()


async def send(version, text, *, token, owner_id, data, out=print, guild_id=None, channel=None):
    text, changelog = split_changelog(text)
    chunks = parts(text)
    if changelog is not None and channel is None:
        raise NotesRefused('a changelog goes with a channel post; the owner\'s DM gets the note alone')
    if changelog is not None and len(chunks) != 1:
        raise NotesRefused(f'a channel post is one message, and this summary needs {len(chunks)}; shorten it '
                           f'(the full changelog is attached)')
    # The split, not just the text: the digest older code computes too, so a rollback can still retry this
    # version. The attached changelog is compared on its own, under `changelog`.
    digest = hashlib.sha256(json.dumps(chunks).encode()).hexdigest()
    hashes = [text_hash(chunk) for chunk in chunks]
    attachment = None if changelog is None else changelog_name(version)
    path = data / ('patch-notes.json' if channel is None else 'patch-notes-server.json')
    with locked(data):
        state = load(path)
        entry = state['versions'].get(version)
        if entry and (entry['sha256'] != digest or len(entry['parts']) != len(chunks) or
                      (changelog is not None and entry.get('changelog') != text_hash(changelog))):
            raise NotesRefused(f'{version} was already sent with different notes; use a new version')
        if (entry and changelog is None and 'changelog' in entry and
                not all(part.get('message_id') for part in entry['parts'])):
            raise NotesRefused(f'{version} goes out with its changelog attached; send the notes with it')
        if entry is None and channel is not None and changelog is None:
            raise NotesRefused(f'a channel post attaches its full changelog: add a {CHANGELOG_MARK} line, then the changelog')
        if entry is None:
            entry = state['versions'][version] = {'sha256': digest, 'parts': [{} for _ in chunks]}
            if changelog is not None:
                entry['changelog'] = text_hash(changelog)
        if any(part.get('sha256', h) != h for part, h in zip(entry['parts'], hashes)):
            raise NotesRefused(f'{path.name} is not a patch-notes record')
        posted = all(part.get('message_id') for part in entry['parts'])
        if posted and channel is None:
            out(f'{version}: all {len(chunks)} parts were already sent')
            return
        # Recovery matches text with its ends trimmed, as Discord stores it, so twins are told by trimmed text.
        entry.setdefault('trimmed', [text_hash(chunk.strip()) for chunk in chunks])
        tried = next((index for index, part in enumerate(entry['parts']) if stage(part) == 1), None)
        twins, unknown = set(), set()
        for other, e in state['versions'].items():
            for at, part in enumerate(e['parts'] if other != version and tried is not None else []):
                if stage(part) != 1:
                    continue
                if 'trimmed' in e:
                    if e['trimmed'][at] == entry['trimmed'][tried]:
                        twins.add(other)
                elif part['sha256'] == entry['parts'][tried]['sha256']:
                    twins.add(other)
                else:
                    unknown.add(other)  # Older code kept no trimmed text: it could still be the same words.
        if twins:
            raise NotesRefused(f'{version} and {", ".join(sorted(twins))} each have a part with this text that was tried but '
                               f'never confirmed, and the history can\'t say whose a message is; check it, then fix {path.name}')
        if unknown:
            private_json(path, state)  # Keeps this version's trimmed text, so the other's retry can tell them apart.
            raise NotesRefused(f'{version} has a part that was tried but never confirmed, and so does {", ".join(sorted(unknown))}, '
                               f'recorded by older code that kept no trimmed text; check them, then fix {path.name}')
        client = discord.Client(intents=discord.Intents.none())
        try:
            await client.login(token)
            try:
                dm = await destination(client, owner_id=owner_id, guild_id=guild_id, channel=channel)
            except Exception as exc:
                if not posted:
                    raise
                raise PinFailed(f'#{channel} could not be read ({type(exc).__name__}: {exc})') from None  # Only the pin was left.
            for index, (chunk, part) in enumerate(zip(chunks, entry['parts']), 1):
                if part.get('message_id'):
                    out(f'part {index}/{len(chunks)}: already sent')
                    continue
                taken = set(sent_ids(state))
                start = 'attempted_at' in part and search_from(state, version, index, part)
                message = 'attempted_at' in part and await landed(dm, client.user.id, chunk, start, taken, attachment)
                if not message:
                    attempted_at = time.time()
                    mark = await anchor(dm, attempted_at)
                    if start is not False:
                        mark = {'attempted_at': attempted_at, 'after': kept(start)}
                    set_anchor(state, version, index, mark)
                    part.update(attempted_at=attempted_at, sha256=hashes[index - 1])
                    private_json(path, state)
                    extra = {} if changelog is None else {
                        'file': discord.File(io.BytesIO(changelog.encode()), filename=attachment)}
                    message = await dm.send(chunk, nonce=nonce(version, digest, index, '' if channel is None else f'#{dm.id}'),
                                            allowed_mentions=NO_MENTIONS, suppress_embeds=True, **extra)
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
            if channel is not None:
                await pin(dm, entry, version, out)
                private_json(path, state)
        finally:
            await client.close()


DRAFT = obj({'notes': STRING})
PATCH_NOTES = '''
Now you're writing a release's patch notes, which you'll post yourself in the server's #fri-chat for
everyone there. Talk to the server, not to one person. The owner is mint, or bossman; mention them in
the third person if at all ("mint finally shipped it"). Use only the facts given: never invent a
feature, fix, number, date or name, and leave out anything members wouldn't notice. Plain words a
member gets: no PR numbers, file names, or words like controller, heartbeat, pin or release script.
It goes out as one Discord message, with the full changelog attached as a file below it, so this is the
short version: one version line, then the few **added** and **fixed** things members would notice most
as short "- " lists (group small fixes into one line), then one **next** line. Under 1500 characters. Same voice as always: lowercase, deadpan, a jab or two,
no exclamation marks, no hype, never cheerful or salesy, no em dashes, few emojis.
Return the whole post as Discord text in `notes`.'''


async def draft(version, facts, *, chat, model, effort, voice_notes=''):
    """Fri's own wording of the maintainer's facts, as the post it would make; nothing is sent."""
    from .fri_agent_model import analyze
    from .fri_chat import VOICE
    instruction = VOICE + ('\nOwner voice notes:\n' + voice_notes if voice_notes else '') + PATCH_NOTES
    result, counters, status = await analyze(chat, instruction, {'version': version, 'facts': facts},
                                             model, effort, DRAFT)
    return (result.get('notes') or '').strip(), counters, status


def drafted(text, status):
    """A draft that came back whole and would send as it is: one message."""
    if status != 'completed' or not text:
        raise SendFailed(f'the draft came back {status}')
    if len(parts(text)) != 1:  # Refused here if it wouldn't send.
        raise NotesRefused('the draft needs more than one message; ask for it again, shorter')
    return text


async def draft_from_controller(version, facts):
    """Model, effort and the owner's voice notes as the controller has them; usage is reported like an eval's."""
    import uuid

    import httpx

    from .fri_chat import Chat
    from .subscription import Subscription
    url = os.getenv('GENE_CONTROLLER_URL', 'http://controller:8787').rstrip('/')
    headers = {'Authorization': 'Bearer '+os.environ['GENE_FRI_TOKEN']}
    chat = Chat(Subscription(os.getenv('FRI_CHATGPT_AUTH', '/auth/session.json')))
    async with httpx.AsyncClient(timeout=30, trust_env=False, headers=headers) as client:
        status, settings = await client.get(url+'/status'), await client.get(url+'/agent/state')
        status.raise_for_status()
        settings.raise_for_status()
        status, config = status.json(), settings.json()['config']
        text, counters, done = await draft(version, facts, chat=chat, model=status['chat_model'],
                                           effort=status['chat_effort'], voice_notes=config.get('voice_notes') or '')
        report = {'attempt_id': uuid.uuid4().hex, 'model': status['chat_model'], 'effort': status['chat_effort'],
                  'status': done, 'counters': counters, 'role': 'evaluation'}
        path = Path(os.getenv('FRI_DATA', '/data'))/'usage'/(report['attempt_id']+'.json')
        private_json(path, report)  # Kept until the controller has it; the live outbox delivers it otherwise.
        posted = await client.post(url+'/chat/usage/report', json=report)
        posted.raise_for_status()
        path.unlink(missing_ok=True)
    return drafted(text, done)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m fri.fri_patch_notes',
                                     description='Draft, preview or send one release\'s patch notes, read from stdin.')
    parser.add_argument('command', choices=('draft', 'send', 'preview', 'pin'))
    parser.add_argument('version', help='the release version, like 0.4.0')
    parser.add_argument('--channel', help='post in this text channel of the server (like fri-chat) instead of '
                                          'the owner\'s DM')
    args = parser.parse_args(argv)
    if not VERSION.fullmatch(args.version):
        parser.error('version must look like 0.4.0')
    if args.channel is not None and (args.command not in ('send', 'pin') or not re.fullmatch(r'[a-z0-9_-]{1,100}', args.channel)):
        parser.error('--channel takes a channel name like fri-chat, with send or pin')
    if args.command == 'pin' and args.channel is None:
        parser.error('pin takes --channel: only channel posts are pinned')
    try:
        if args.command == 'pin':
            token, guild = os.getenv('DISCORD_TOKEN', ''), os.getenv('GENE_GUILD_ID', '')
            if not token or not ID.fullmatch(guild):
                raise NotesRefused('DISCORD_TOKEN and GENE_GUILD_ID must be set')
            asyncio.run(pin_posted(args.version, token=token, data=Path(os.getenv('FRI_DATA', '/data')),
                                   guild_id=int(guild), channel=args.channel))
            return 0
        try:
            text = sys.stdin.buffer.read().decode('utf-8')
        except UnicodeDecodeError:
            raise NotesRefused('the notes are not UTF-8') from None
        if args.command == 'preview':
            summary, changelog = split_changelog(text)
            for index, chunk in enumerate(parts(summary), 1):
                print(f'--- part {index} ({length(chunk)} chars)\n{chunk}')
            if changelog is not None:
                print(f'--- attached {changelog_name(args.version)} ({len(changelog.encode())} bytes, '
                      f'{changelog.count(chr(10))} lines)\n{changelog}', end='')
            return 0
        if args.command == 'draft':
            if not text.strip():
                raise NotesRefused('the facts are empty')
            print(asyncio.run(draft_from_controller(args.version, text)))
            return 0
        token, owner, guild = os.getenv('DISCORD_TOKEN', ''), os.getenv('GENE_OWNER_ID', ''), os.getenv('GENE_GUILD_ID', '')
        if not token or not ID.fullmatch(owner):
            raise NotesRefused('DISCORD_TOKEN and GENE_OWNER_ID must be set')
        if args.channel is not None and not ID.fullmatch(guild):
            raise NotesRefused('GENE_GUILD_ID must be set to post in a channel')
        asyncio.run(send(args.version, text, token=token, owner_id=int(owner), data=Path(os.getenv('FRI_DATA', '/data')),
                         guild_id=int(guild) if args.channel is not None else None, channel=args.channel))
    except NotesRefused as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 2
    except PinFailed as exc:
        print(f'posted, not pinned: {exc}', file=sys.stderr)
        return 3
    except Exception as exc:  # Discord, the network or the host failed mid-send, in whatever form.
        # The record is written before each send and after each confirmation, so a rerun is safe.
        print(f'failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
