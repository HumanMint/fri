"""Closing the loop when Gene gets fixed.

A Gene error Fri files gets a 👀, and its job is watched for three days after the error. Once that fix
ships, Fri says one dry line under the error, when the room is quiet, and never a second one for it.
Only deployed counts: a failure, a rejection, a withdrawal or a hand-off to the owner is never
announced, and nothing is while Gene's running release is unknown. The owner's DM for a shipped
repair that a member reported gets a Tell reporter button, which replies to that report once. If what the
controller checked stopped holding while the reply went out, Fri takes the reply back.

A controller that offers it has Fri ask for the send itself once its last Discord read is done, so the
reporter's erasure or a release landing meanwhile stops the reply before it posts (fri #214). The send is
noted first, and a heartbeat pass settles one a lost answer or a shutdown left, whatever the controller
offers by then: it records what happened and takes back a reply that no longer holds. A send whose answer
was lost is known only by Discord's own copy of it, which Fri's gateway delivers with its claim's nonce
(fri #221): seen, it's recorded as sent and so checked again, and unseen it stays unconfirmed, since nothing
in Discord tells the notice from another reply of Fri's to the report. Seen only once a pass was recording it as
unconfirmed, which the controller never checks again, it's taken back (fri #232), and so it is once that's recorded:
the claim's nonce and the channel are kept for a day, so a copy that comes later still is (fri #238). A take-back
Discord refuses with a 403 is tried again until then, since Fri may be let again, and the owner hears of it once.
"""

import asyncio
import collections
import json
import logging
import re
import time
import weakref

import discord
import httpx

from . import feedback as commands
from .fri_errors import ControllerRejected
from .fri_notices import notice_text
from .private_files import private_json

LOG = logging.getLogger('fri.fixes')
NO_MENTIONS = discord.AllowedMentions.none()
FILE = 'gene-fixes.json'
WATCHING = 3*86400  # After this a "fixed" line under Gene's error reads as necro-posting, so the watch just ends.
QUIET = 60  # Someone spoke this recently: wait for a lull instead of talking over them.
PASS = 10  # How long a heartbeat waits on a pass over the watch list before moving on. It never cancels it.
MARK = 5  # How long filing an error waits on its 👀 before the attention stage moves on. It never cancels it.
RUNNING = weakref.WeakKeyDictionary()  # Each agent's pass, so a slow one is waited on again instead of doubled.
MARKING = set()  # 👀 still on their way after attention moved on, kept so none is dropped unfinished.
SETTLED = frozenset({'deployed', 'rejected', 'failed', 'needs_attention'})
CAPABILITY = 'requester-notices'
RECHECK = 'requester-notice-recheck'  # The controller says when a sent reply's claim stopped holding (gene #47).
AUTHORIZE = 'requester-notice-authorize'  # Fri asks for the send right before it (fri #214).
# Sends a tap asked for, until what happened is recorded and done: the lease, the owner who tapped, where it went,
# Fri's own message once it's out, and times. Nothing of the report's.
PENDING = 'requester-notices.json'
GAVE_UP = 60  # Fri gives a send 25 s: noted this long ago with no tap running, it has posted or never will.
KEEP = 86400  # A noted send still unsettled after this (a channel gone, say) is dropped, and logged.
ECHOES = 20  # Sends settled unconfirmed whose copy a later take-back can still match (see bury): the newest only.
TELLING = collections.Counter()  # Taps replying for each job in this process, which a pass leaves alone.
TAKE_BACK = 10  # How long a take-back waits on Discord's answer. It never cancels the delete (see retract).
DELETING = {}  # Each delete still on its way after its take-back moved on, by message, so none is doubled.
SETTLING = weakref.WeakKeyDictionary()  # Each agent's settling pass, waited on like check's.
JOB = re.compile(r'[a-f0-9]{12}')
TELL_ID = re.compile(r'^fri:tell:(?P<job_id>[a-f0-9]{12})$')
SOURCE = re.compile(r'https://discord\.com/channels/(?P<guild>[0-9]{1,25}|@me)/(?P<channel>[0-9]{1,25})/(?P<message>[0-9]{1,25})')
MALFORMED = (KeyError, TypeError, ValueError, OverflowError)
# What a tap can hit besides a definite refusal: controller data of the wrong shape, a Discord object
# that changed under it, a transport that gave up. The owner still gets an answer and a claim is finished.
TAP_FAILURES = (ValueError, KeyError, TypeError, AttributeError, TimeoutError, OSError, discord.DiscordException)


def fixed_line(sha):
    return f'that one should be fixed now, gene’s on `{sha[:7]}`. you’re welcome'


def told_line(sha):
    return f'that gene thing you reported should be fixed now (`{sha[:7]}`). try not to break it again'


def watched(fri):
    try:
        entries = json.loads((fri.data/FILE).read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, RecursionError, OSError) as exc:
        LOG.warning('Gene fix watch list unreadable (%s); starting it over', type(exc).__name__)
        return {}
    return entries if isinstance(entries, dict) else {}


def expired(entry):
    """Timed from Gene's error itself, so refiling an old error never restarts the three days."""
    return time.time()-discord.utils.snowflake_time(int(entry['message_id'])).timestamp() > WATCHING


def save(fri, job_id, entry):
    """Read again just before each write: a filing may have added to the list while a pass awaited."""
    entries = watched(fri)
    if entry is None:
        entries.pop(job_id, None)
    else:
        entries[job_id] = entry
    private_json(fri.data/FILE, entries)


async def follow(agent, channel_id, message_id, job):
    """A Gene error Fri just filed: watch its job and put a 👀 on the error. Never raises."""
    job_id = str((job or {}).get('id', ''))
    entry = {'channel_id': str(channel_id), 'message_id': str(message_id)}
    try:
        if not JOB.fullmatch(job_id) or job.get('state') in SETTLED or expired(entry):
            return  # A refiled error whose job already ended has nothing left to announce.
    except MALFORMED:
        return
    entries = watched(agent.fri)
    # One watch per error, kept until its three days end: filing it again through the other path (a repair,
    # then a request) opens a second job, and that one must not get a second line.
    if job_id in entries or any(isinstance(other, dict) and str(other.get('message_id')) == entry['message_id']
                                for other in entries.values()):
        return
    entries[job_id] = entry
    try:
        private_json(agent.fri.data/FILE, entries)
    except OSError as exc:
        LOG.warning('Gene fix watch not saved: %s', type(exc).__name__)
    # Attention calls this from the heartbeat, which a Discord read held up by a rate limit must not stall.
    await asyncio.wait({eyes(agent, job_id, entry)}, timeout=MARK)


def eyes(agent, job_id, entry):
    """The 👀 as its own task, never cancelled (see check), or the one already on its way, so none is doubled."""
    name = 'fri-mark:'+job_id
    for task in MARKING:
        if task.get_name() == name and not task.done():
            return task
    task = asyncio.create_task(mark(agent, job_id, entry['channel_id'], entry['message_id']), name=name)
    MARKING.add(task)
    task.add_done_callback(marked)
    return task


async def mark(agent, job_id, channel_id, message_id):
    """Noted in the watch once tried, so a pass adds one a shutdown cut off, and never retries one Discord refused."""
    await react(agent, channel_id, message_id)
    entries = watched(agent.fri)
    if isinstance(entries.get(job_id), dict):  # A pass may have ended the watch meanwhile; it stays ended.
        entries[job_id]['marked'] = True
        private_json(agent.fri.data/FILE, entries)


async def react(agent, channel_id, message_id):
    """Its own reaction on Gene's own message, not acknowledge(): that one is for members' turns."""
    channel = agent.fri.get_channel(int(channel_id))
    if channel is None or getattr(channel, 'guild', None) is None or not agent.allowed(channel):
        return
    permissions = channel.permissions_for(channel.guild.me)
    if not (permissions.view_channel and permissions.read_message_history and permissions.add_reactions):
        return
    try:
        message = await channel.fetch_message(int(message_id))
        if message.author.id == agent.gene_id and not message.webhook_id:
            await message.add_reaction('👀')
    except discord.HTTPException:
        LOG.info('Gene error mark was not confirmed')


def marked(task):
    MARKING.discard(task)
    if not task.cancelled() and task.exception() is not None:
        LOG.info('Gene error mark ended: %s', type(task.exception()).__name__)


def can_send(channel):
    try:
        permissions = channel.permissions_for(channel.guild.me)
    except discord.ClientException:  # A thread whose parent channel was deleted.
        return False
    return (permissions.view_channel and permissions.read_message_history and
            (permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages))


def room(agent, entry):
    """Where the line goes, while Fri may post there: not a channel it left or an archived thread it'd reopen."""
    channel = agent.fri.get_channel(int(entry['channel_id']))
    if (channel is None or getattr(channel, 'guild', None) is None or getattr(channel, 'archived', False) or
            not agent.allowed(channel) or not can_send(channel)):
        return None
    return channel


async def due(agent, job_id, entry):
    """'keep', 'drop', or the channel, Gene's message and the live release to post under it."""
    fri = agent.fri
    if expired(entry):
        return 'drop'
    if not (agent.config['enabled'] and agent.config['proactive']) or room(agent, entry) is None:
        return 'keep'  # No reads while Fri couldn't post anyway; the three days still run.
    try:
        job = await fri.api('GET', '/requests/'+job_id)
    except ControllerRejected as exc:
        return 'drop' if exc.status in (403, 404) else 'keep'
    if job.get('state') != 'deployed':
        return 'drop' if job.get('state') in SETTLED else 'keep'
    sha = str(job.get('approved_sha') or '')
    if not sha:
        return 'drop'
    channel = room(agent, entry)
    if channel is None:
        return 'keep'
    try:
        message = await channel.fetch_message(int(entry['message_id']))
    except discord.NotFound:
        return 'drop'
    except discord.HTTPException:
        return 'keep'
    if message.author.id != agent.gene_id:
        return 'drop'
    # The release is read last and every gate after it needs no await, so a rollback, an archive, a
    # config change or proactive posts going off while Discord answered still stops the line.
    status = await fri.api('GET', '/status')
    if status.get('active_sha') != sha:
        return 'drop'  # A rollback or a newer release came first, so "fixed" might not hold.
    if status.get('runtime_unknown'):
        return 'keep'  # A failed restore never proved healthy: Gene may not be on it (gene #48). Said once known.
    channel = room(agent, entry)
    if not (agent.config['enabled'] and agent.config['proactive']) or channel is None:
        return 'keep'  # Paused, or proactive posts off, while Discord answered.
    last = channel.last_message_id
    if last and time.time()-discord.utils.snowflake_time(last).timestamp() < QUIET:
        return 'keep'
    return channel, message, sha


async def check(agent):
    """Heartbeat stage: waits up to PASS on a pass over the watch list, and fails the stage if it's still going.

    A Discord read is never cancelled: discord.py can't recover from a cancel inside its global rate-limit
    wait, and every later Discord call would stall. A pass still running is left to finish, and the next
    heartbeat waits on it again instead of starting another."""
    task = RUNNING.get(agent)
    if task is None or task.done():
        task = RUNNING[agent] = asyncio.create_task(sweep(agent))
        task.add_done_callback(ended)
    done, _ = await asyncio.wait({task}, timeout=PASS)
    if not done:
        raise TimeoutError('Gene fix pass still running')
    task.result()


def ended(task):
    """A pass that fails after its heartbeat moved on is still logged, not left unretrieved."""
    if not task.cancelled() and task.exception() is not None:
        LOG.info('Gene fix pass ended: %s', type(task.exception()).__name__)


async def sweep(agent):
    """One pass: each watched job is read until it settles, and a shipped fix is announced once."""
    from .fri_workspace import send_once
    for job_id, entry in watched(agent.fri).items():
        try:
            if not JOB.fullmatch(job_id) or not isinstance(entry, dict):
                outcome = 'drop'
            elif entry.get('done'):
                outcome = 'drop' if expired(entry) else 'keep'  # Kept only so a refile isn't watched again.
            else:
                outcome = await due(agent, job_id, entry)
                if outcome == 'keep' and not entry.get('marked') and agent.config['enabled']:
                    eyes(agent, job_id, entry)  # A 👀 a shutdown cut off. Not waited on: a pass never stalls on one.
        except MALFORMED:
            outcome = 'drop'  # A malformed entry would fail every heartbeat.
        if outcome == 'keep':
            continue
        if outcome == 'drop':
            save(agent.fri, job_id, None)
            continue
        channel, message, sha = outcome
        save(agent.fri, job_id, {**entry, 'done': True})  # Before the send: an unconfirmed post is never repeated.
        try:
            await send_once(agent.fri, {'id': job_id, 'target_id': str(channel.id), 'body': fixed_line(sha)},
                            reply_to=message.id)
        except (httpx.HTTPError, ValueError, TimeoutError, OSError) as exc:
            LOG.info('Gene fix line was not confirmed: %s', type(exc).__name__)


def offers_tell(fri, notice):
    """The deployed DM of a repair a member reported, with the report still on record."""
    job = notice.get('job') or {}
    bots = {str(getattr(getattr(fri, 'user', None), 'id', '')), str(getattr(getattr(fri, 'agent', None), 'gene_id', ''))}
    return bool(notice.get('phase') == 'status' and job.get('state') == 'deployed' and
                CAPABILITY in getattr(fri, 'capabilities', ()) and str(job.get('source_id', '')).startswith('repair:') and
                SOURCE.fullmatch(job.get('source_url') or '') and job.get('approved_sha') and
                str(job.get('user_id', '')).isdigit() and str(job['user_id']) not in bots)


def tell_component(interaction, bot_id, owner_id):
    """Only the owner's Tell reporter buttons on Fri's own DMs."""
    if not bot_id or not owner_id or getattr(interaction, 'guild_id', None) is not None:
        return False
    if str(getattr(getattr(interaction, 'user', None), 'id', '')) != str(owner_id):
        return False
    kind = getattr(interaction, 'type', None)
    if getattr(kind, 'value', kind) != 3:
        return False
    if getattr(getattr(getattr(interaction, 'message', None), 'author', None), 'id', None) != bot_id:
        return False
    data = getattr(interaction, 'data', None)
    custom_id = data.get('custom_id') if isinstance(data, dict) else None
    return isinstance(custom_id, str) and TELL_ID.fullmatch(custom_id) is not None


class Unreachable(Exception):
    pass


def postable(fri, channel):
    """Every gate that needs no await, so it runs again with nothing awaited before the send."""
    config = fri.agent.config
    if not config['enabled']:
        raise Unreachable('i’m switched off right now')
    if not config['proactive']:
        raise Unreachable('proactive posts are off')
    if channel is None:
        return
    if isinstance(channel, discord.DMChannel):
        if not config['support_dms']:
            raise Unreachable('support dms are off')
        return
    if getattr(channel, 'guild', None) is None or not fri.agent.allowed(channel) or not can_send(channel):
        raise Unreachable('i can’t post where they reported it anymore')
    if getattr(channel, 'archived', False):
        raise Unreachable('their thread’s archived, and i’m not reopening it for this')


async def live(fri, job):
    """The controller checks this in its claim too; an older one (gene #40) doesn't, so Fri reads the live release."""
    status = await fri.api('GET', '/status')
    if status.get('active_sha') != job['approved_sha']:
        raise Unreachable('a newer release is live now, so "fixed" might not hold')
    if (status.get('jobs') or {}).get('deploying'):
        raise Unreachable('another release is going out right now. tap again once it lands')
    if status.get('runtime_unknown'):  # A rollback failed after the claim: what's running isn't known.
        raise Unreachable("gene can't confirm what's running right now, so \"fixed\" might not hold")


async def report(fri, job):
    """The reporter's message and where it is, read before the claim: nothing has been sent yet.
    Also whether it's a thread Fri had to fetch, which no archive update reaches."""
    source = job.get('source_url') if isinstance(job, dict) else None
    match = SOURCE.fullmatch(source) if isinstance(source, str) else None
    user = str(job.get('user_id', '')) if match else ''
    if (not match or job.get('state') != 'deployed' or not job.get('approved_sha') or not user.isdigit() or
            user in {str(fri.user.id), str(fri.agent.gene_id)}):
        raise Unreachable('there’s no member report on record to reply to')
    postable(fri, None)
    try:
        if match['guild'] == '@me':
            channel = await fri.fetch_channel(int(match['channel']))
            if not isinstance(channel, discord.DMChannel) or not channel.recipient or str(channel.recipient.id) != user:
                raise Unreachable('that dm isn’t theirs anymore')
        else:
            cached = fri.get_channel(int(match['channel']))
            channel = cached or await fri.fetch_channel(int(match['channel']))
            if int(match['guild']) != fri.guild_id or getattr(channel, 'guild', None) is None:
                raise Unreachable('i can’t post where they reported it anymore')
        postable(fri, channel)
        message = await channel.fetch_message(int(match['message']))
        fetched = match['guild'] != '@me' and cached is None and isinstance(channel, discord.Thread)
        if fetched:  # Not in the cache, so read it again last.
            channel = await fri.fetch_channel(channel.id)
        # Last of the Discord reads: no members intent tells Fri someone left while the others ran.
        if not await fri.agent.member(int(user), fresh=True):
            raise Unreachable('they’re not in the server anymore')
    except discord.NotFound:
        raise Unreachable('their report is gone') from None
    except discord.HTTPException:
        raise Unreachable('discord wouldn’t let me read their report') from None
    if str(message.author.id) != user or message.author.bot or message.webhook_id:
        raise Unreachable('that report isn’t theirs anymore')
    postable(fri, channel)  # Again after the last read, so a change while Discord answered claims nothing.
    return channel, message, fetched


async def tell(fri, job_id, actor):
    """Read Discord, then claim the controller's mark, reply once and record what happened.

    The claim checks erasure, rollbacks and the live release, so nothing that changed while Discord answered
    can slip through. With it held, Fri reads a thread it had to fetch once more, then asks the controller for
    the send, which checks all that again (fri #214); an older controller has Fri read the live release itself
    instead. A release, an erasure or an archive while the claim was out stops the reply too. What the owner
    sees, and whether the reporter was told."""
    TELLING[job_id] += 1  # Counted: a second tap that ends first mustn't uncover the first.
    try:
        return await tell_once(fri, job_id, actor)
    finally:
        TELLING[job_id] -= 1
        if TELLING[job_id] <= 0:
            del TELLING[job_id]


async def tell_once(fri, job_id, actor):
    from .fri_workspace import send_once
    scope = {'guild_id': str(fri.guild_id), 'user_id': str(actor)}
    # An older controller ignores a field it doesn't know, and would take a prepared claim as cleared to send.
    authorizes = AUTHORIZE in getattr(fri, 'capabilities', ())
    try:
        job = await fri.api('GET', '/requests/'+job_id)
        channel, message, fetched = await report(fri, job)
        await live(fri, job)
        postable(fri, channel)
        claimed = await fri.api('POST', f'/requests/{job_id}/requester-notice',
                                {**scope, 'prepare': True} if authorizes else scope)
    except Unreachable as exc:
        return f'didn’t send it: {exc}.', False
    except commands.UserFeedbackCheckFailure as exc:
        return notice_text(str(exc)), False
    except TAP_FAILURES:
        return 'didn’t send it. tap again later.', False  # Nothing claimed yet, so there's nothing to record.
    lease = claimed.get('lease') if isinstance(claimed, dict) else None
    if not isinstance(lease, str) or not lease:
        LOG.warning('Requester notice claim for %s came back without a lease; nothing sent', job_id)
        return 'didn’t send it. tap again later.', False
    state, message_id, sending, entry = 'failed', '', False, None
    try:
        if not isinstance(claimed.get('job'), dict) or any(
                str(claimed['job'].get(key)) != str(job.get(key)) for key in ('user_id', 'source_url', 'approved_sha')):
            raise Unreachable('their report changed while i looked. tap again')
        if not authorizes:
            await live(fri, job)
        if fetched:
            channel = await fri.fetch_channel(channel.id)
        postable(fri, channel)  # Config or the channel may have changed while the claim was out.
        if authorizes:
            # Noted before the send is asked for: a shutdown from here on leaves the next pass something to settle.
            entry = {'lease': lease, 'actor': str(actor), 'channel_id': str(channel.id), 'since': time.time(),
                     'at': time.time()}
            note(fri, job_id, entry)
            await cleared(fri, job_id, scope, lease)
            entry = {**entry, 'cleared': True}  # From here a shutdown may leave a reply up: unconfirmed, not failed.
            note(fri, job_id, entry)  # Raises if it can't be noted, and then nothing is sent.
            postable(fri, channel)  # Again, after the controller's answer: nothing is awaited from here to the send.
        sending = True
        result = await send_once(fri, {'id': notice_id(job_id, lease), 'target_id': str(channel.id),
                                       'body': told_line(job['approved_sha'])}, reply_to=message.id)
        message_id, state = str(int(result['id'])), 'sent'
        if entry:
            entry = keep(fri, job_id, {**entry, 'message_id': message_id})
        text = f'told them: {message.jump_url}'
    except Unreachable as exc:
        text = f'didn’t send it: {exc}.'
    except commands.UserFeedbackCheckFailure as exc:  # The controller's answer to the release read; nothing sent.
        text = notice_text(str(exc))
    except httpx.HTTPStatusError as exc:
        # Discord refused it, so nothing posted and a later tap may try again. A 5xx may have posted.
        state = 'uncertain' if exc.response.status_code >= 500 else 'failed'
        text = (f'discord refused the reply (HTTP {exc.response.status_code}). tap again later.' if state == 'failed'
                else 'discord didn’t confirm the reply, so it might have posted. not sending it twice.')
    except (httpx.HTTPError, *TAP_FAILURES):
        state = 'uncertain' if sending else 'failed'
        text = ('discord didn’t confirm the reply, so it might have posted. not sending it twice.' if sending
                else 'didn’t send it. tap again later.')
    if state == 'uncertain' and entry:  # Its answer lost, the notice is known only by Discord's copy (see seen).
        current = noted(fri).get(job_id)
        if not (isinstance(current, dict) and current.get('lease') == lease and current.get('message_id')):
            return text, False  # Not seen yet: the next pass records it, as sent once seen (see settle_one).
        entry, message_id, state, text = current, str(current['message_id']), 'sent', f'told them: {message.jump_url}'
    recorded, answered = None, False
    try:
        recorded = await fri.api('POST', f'/requests/{job_id}/requester-notice/finish',
                                 {**scope, 'lease': lease, 'state': state, 'message_id': message_id})
        answered = True
    except (commands.UserFeedbackCheckFailure, *TAP_FAILURES) as exc:  # An answer Fri can't read is still unconfirmed.
        answered = isinstance(exc, ControllerRejected)  # Refused: the same again would be too.
        LOG.warning('Requester notice outcome for %s not recorded; it stays unretried', job_id)
    if (state == 'sent' and RECHECK in getattr(fri, 'capabilities', ()) and isinstance(recorded, dict) and
            recorded.get('state') == 'invalidated'):
        if entry:
            entry = keep(fri, job_id, {**entry, 'take_back': True})
        outcome = await retract(channel, message_id)
        if entry and outcome == 'forbidden':  # This answer tells the owner, so the passes trying again stay quiet.
            keep(fri, job_id, {**entry, 'told': True})
        elif entry and outcome != 'unconfirmed':
            forget(fri, job_id, lease)
        return taken_back(outcome, recorded.get('reason'), message, retried=entry is not None), False
    if entry and answered:  # Otherwise the next pass hears the answer again (see settle).
        forget(fri, job_id, lease)
    return text, state == 'sent'


async def cleared(fri, job_id, scope, lease):
    """The controller's go for the send, which checks the claim, the reporter's privacy and the release again."""
    try:
        await fri.api('POST', f'/requests/{job_id}/requester-notice/authorize', {**scope, 'lease': lease})
    except ControllerRejected as exc:
        raise Unreachable(notice_text(str(exc)).strip().rstrip('.')[:300] or 'gene said no') from None


def notice_id(job_id, lease):
    """The notice's post id: its job's, then its claim's, so the nonce send_once gives it is this claim's alone,
    never the "fixed now" line's (its job's) or another claim's. A lease that isn't hex adds nothing (see seen)."""
    return job_id+lease[:8] if re.fullmatch(r'[0-9a-f]{8,}', str(lease)) else job_id


def seen(fri, message):
    """Fri's own message as the gateway delivered it: the notice of a cleared send whose answer was lost, if it
    carries that claim's nonce in that channel (fri #221). Noted as sent, so the tap or the next pass records it,
    the controller checks it again and Fri takes it back if it no longer holds. Nothing else of Fri's matches:
    each claim's nonce is its own. One settled unconfirmed by then is taken back (see bury). Never raises."""
    nonce = getattr(message, 'nonce', None)
    if nonce is None or getattr(getattr(message, 'author', None), 'id', None) != getattr(fri.user, 'id', None):
        return
    try:
        for job_id, entry in noted(fri).items():
            if (not isinstance(entry, dict) or entry.get('message_id') or
                    str(message.channel.id) != entry.get('channel_id')):
                continue
            if 'nonce' in entry:
                if str(nonce) == entry['nonce']:
                    keep(fri, job_id, {**entry, 'message_id': str(int(message.id)), 'take_back': True})
            elif (entry.get('cleared') and (notice := notice_id(job_id, entry.get('lease'))) != job_id and
                    str(nonce) == str(int(notice[:20], 16))):
                keep(fri, job_id, {**entry, 'message_id': str(int(message.id))})
    except (*MALFORMED, AttributeError):
        LOG.info('A requester notice copy could not be matched')


async def retract(channel, message_id):
    """Fri's own reply down: 'gone' once it is (or already was), 'refused' when Discord won't, 'forbidden' when
    it won't for now (a 403: Fri's permissions may come back), 'unconfirmed' when Discord didn't answer. A later
    try may settle the last two.

    Waits up to TAKE_BACK and never cancels the delete: discord.py can't recover from a cancel inside its global
    rate-limit wait, and every later Discord call would stall (fri #226). One still going is left to finish, and
    a later try waits on it again instead of starting another."""
    try:
        task = deleting(channel, int(message_id))
    except (ValueError, KeyError, TypeError, AttributeError):
        return 'refused'
    done, _ = await asyncio.wait({task}, timeout=TAKE_BACK)
    if not done:
        return 'unconfirmed'
    try:
        task.result()
    except discord.NotFound:
        return 'gone'
    except discord.Forbidden:
        return 'forbidden'
    except discord.HTTPException as exc:
        return 'unconfirmed' if exc.status >= 500 else 'refused'
    except (TimeoutError, OSError, discord.DiscordException):
        return 'unconfirmed'
    except (ValueError, KeyError, TypeError, AttributeError):
        return 'refused'
    return 'gone'


def deleting(channel, message_id):
    """The delete as its own task, or the one already on its way for that message, like eyes."""
    task = DELETING.get(message_id)
    if task is None or task.done():
        task = DELETING[message_id] = asyncio.create_task(channel.get_partial_message(message_id).delete(),
                                                          name=f'fri-take-back:{message_id}')
        task.add_done_callback(lambda done: deleted(message_id, done))
    return task


def deleted(message_id, task):
    """A delete that ends after its take-back moved on is still retrieved, and the next try starts afresh."""
    if DELETING.get(message_id) is task:
        del DELETING[message_id]
    if not task.cancelled() and task.exception() is not None:
        LOG.info('Requester notice delete ended: %s', type(task.exception()).__name__)


def taken_back(outcome, reason, report, retried=False):
    """The controller's checks stopped holding while the reply went out (a rollback, a newer release, the
    reporter's erasure), so it never recorded it as sent. retried: a later pass tries again (see settle)."""
    why = reason.strip().rstrip('.')[:300] if isinstance(reason, str) and reason.strip() else ''
    why = why or 'gene’s checks stopped holding while it went out'
    if outcome == 'gone':
        return notice_text(f'took the reply back: {why}.')
    LOG.warning('A reply whose claim stopped holding was not taken back yet')
    if outcome in ('unconfirmed', 'forbidden') and retried:
        refusal = 'didn’t confirm taking it back' if outcome == 'unconfirmed' else 'wouldn’t let me take it back'
        return notice_text(f'it posted, but {why}, and discord {refusal}. i’ll keep trying: {report.jump_url}')
    return notice_text(f'it posted, but {why}, and discord wouldn’t let me take it back: {report.jump_url}')


def noted(fri):
    try:
        entries = json.loads((fri.data/PENDING).read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, RecursionError, OSError) as exc:
        LOG.warning('Requester notice sends unreadable (%s)', type(exc).__name__)
        return {}
    return entries if isinstance(entries, dict) else {}


def note(fri, job_id, entry):
    """Read again just before each write, like save. Raises if it can't be written: a send isn't cleared then."""
    entries = noted(fri)
    entries[job_id] = entry
    private_json(fri.data/PENDING, entries)


def keep(fri, job_id, entry):
    """note, once the send is out: a failed write is logged, and the tap still records what happened."""
    try:
        note(fri, job_id, entry)
    except OSError as exc:
        LOG.warning('Requester notice send not noted: %s', type(exc).__name__)
    return entry


def bury(fri, job_id, entry):
    """A cleared send settled unconfirmed, its copy still unseen: what's kept is what a copy that comes later needs
    to be taken back (see seen), the claim's nonce, its lease's first digits and the channel, until KEEP from the tap.
    Nothing of the owner's or the reporter's, and only the newest ECHOES (fri #238). Raises if it can't be written."""
    entries = noted(fri)
    current = entries.get(job_id)
    if not (isinstance(current, dict) and current.get('lease') == entry['lease']):
        return  # Gone already, or another claim's now.
    notice = notice_id(job_id, entry['lease'])
    if notice == job_id:  # No nonce of its own, so no copy could be told apart.
        del entries[job_id]
    else:
        entries[job_id] = {'nonce': str(int(notice[:20], 16)), 'lease': entry['lease'][:8],
                           'channel_id': str(entry['channel_id']), 'at': entry['at']}
        buried = sorted((key for key, kept in entries.items()
                         if isinstance(kept, dict) and 'nonce' in kept and not kept.get('message_id')),
                        key=lambda key: when(entries[key]))
        for key in buried[:max(len(buried)-ECHOES, 0)]:
            del entries[key]
    private_json(fri.data/PENDING, entries)


def when(entry):
    try:
        return float(entry['at'])
    except MALFORMED:
        return 0.0


def stale(job_id, entry):
    """A note past KEEP, or one this Fri can't read: nothing's left to settle, so it goes."""
    try:
        return not JOB.fullmatch(job_id) or not isinstance(entry, dict) or time.time()-float(entry['at']) > KEEP
    except MALFORMED:
        return True


def expire(fri):
    """Stale notes dropped before the heartbeat waits on the controller, whose outage would otherwise keep their
    channel and message IDs for as long as it lasts (fri #238). A tap's own is left to it. Raises if the write fails."""
    entries = noted(fri)
    kept = {job_id: entry for job_id, entry in entries.items() if TELLING.get(job_id) or not stale(job_id, entry)}
    if len(kept) < len(entries):
        LOG.warning('Gave up settling %d requester notice send(s)', len(entries)-len(kept))
        private_json(fri.data/PENDING, kept)


def forget(fri, job_id, lease):
    """Only this lease's: a later tap may have noted its own since. A malformed one goes whatever its lease."""
    entries = noted(fri)
    entry = entries.get(job_id)
    if job_id in entries and (not isinstance(entry, dict) or entry.get('lease') == lease):
        entries.pop(job_id)
        try:
            private_json(fri.data/PENDING, entries)
        except OSError as exc:
            LOG.warning('Requester notice send not forgotten: %s', type(exc).__name__)


async def settle(agent):
    """Heartbeat stage, whether Fri is switched on or not and whatever the controller offers: taking a reply back
    is never a post, and a note can outlive the controller that cleared its send. Waited on like check, and
    never cancelled."""
    task = SETTLING.get(agent)
    if task is None or task.done():
        if not noted(agent.fri):
            return
        task = SETTLING[agent] = asyncio.create_task(settle_all(agent.fri))
        task.add_done_callback(ended)
    done, _ = await asyncio.wait({task}, timeout=PASS)
    if not done:
        raise TimeoutError('Requester notice pass still running')
    task.result()


async def settle_all(fri):
    """Each cleared send whose tap never saw it through: a lost answer, a shutdown, a take-back cut off."""
    for job_id, entry in noted(fri).items():
        if TELLING.get(job_id):
            continue
        try:
            if stale(job_id, entry):
                LOG.warning('Gave up settling a requester notice send')
                done = True
            else:
                done = await settle_one(fri, job_id, entry)
        except MALFORMED:
            done = True
        except (commands.UserFeedbackCheckFailure, discord.DiscordException, TimeoutError, OSError) as exc:
            LOG.info('Requester notice send not settled yet: %s', type(exc).__name__)
            continue
        if done:
            forget(fri, job_id, entry.get('lease') if isinstance(entry, dict) else None)


async def settle_one(fri, job_id, entry):
    """Record what a noted send did, then take it back if it no longer holds. True once nothing's left to do.

    A send cut off before Fri heard its outcome is recorded as unconfirmed once it was cleared (it may be up),
    and as failed before then: Fri never sends without the controller's go. One whose copy Fri saw is recorded
    as sent (see seen), so the controller checks it again. One whose copy came only once it was being recorded as
    unconfirmed is taken back: that record is final, so nothing checks it now (fri #232). Unseen by then, what its
    copy needs is kept (see bury). A take-back Discord refuses for now is tried again, and the owner told once."""
    message_id = str(entry.get('message_id') or '')
    if 'nonce' in entry and not message_id:
        return False  # Settled unconfirmed: only its copy, if it comes, leaves anything to do.
    if not message_id and time.time()-float(entry['since']) < GAVE_UP:
        return False  # A send from a tap that just stopped may still land.
    if not entry.get('take_back'):
        state = 'sent' if message_id else 'uncertain' if entry.get('cleared') else 'failed'
        try:
            recorded = await fri.api('POST', f'/requests/{job_id}/requester-notice/finish',
                                     {'guild_id': str(fri.guild_id), 'user_id': str(entry['actor']),
                                      'lease': entry['lease'], 'state': state, 'message_id': message_id})
        except ControllerRejected:
            return True  # No claim of this lease (another tap's, or recorded), or no such route: an older controller.
        current = noted(fri).get(job_id)  # Read again: its copy may have come while the finish was out (see seen).
        if (not message_id and isinstance(current, dict) and current.get('lease') == entry['lease'] and
                current.get('message_id')):
            entry, message_id = current, str(current['message_id'])
        held = recorded.get('state') if isinstance(recorded, dict) else None
        # Up, and either no longer holding or recorded unconfirmed, which the controller never checks again.
        if not (message_id and (held == 'uncertain' or state == 'sent' and held == 'invalidated')):
            if state != 'uncertain' or message_id:
                return True
            bury(fri, job_id, entry)
            return False
        entry = keep(fri, job_id, {**entry, 'take_back': True})
    channel = await reach(fri, entry)
    outcome = await retract(channel, message_id)
    if outcome == 'forbidden':
        if not entry.get('told'):
            LOG.warning('Discord refused to take back a reply whose claim stopped holding; trying again later')
            await warn_owner(fri, job_id, entry, channel, message_id)
        return False
    if outcome == 'refused':
        LOG.warning('A reply whose claim stopped holding could not be taken back')
    return outcome != 'unconfirmed'


async def warn_owner(fri, job_id, entry, channel, message_id):
    """The owner hears once where the reply Discord won't let Fri take back is, and the passes keep trying."""
    try:
        owner = fri.get_user(fri.owner_id) or await fri.fetch_user(fri.owner_id)
        dm = owner.dm_channel or await owner.create_dm()
    except TAP_FAILURES as exc:  # Not told yet, so the next pass, trying again, tells them then.
        LOG.info('Owner DM not reached for a refused take-back: %s', type(exc).__name__)
        return
    keep(fri, job_id, {**entry, 'told': True})  # At most once: a DM whose answer was lost isn't repeated.
    guild = getattr(getattr(channel, 'guild', None), 'id', None) or '@me'
    try:
        await dm.send(notice_text('my reply to a reporter has to come down, and discord won’t let me delete it. '
                                  f'i’ll keep trying: https://discord.com/channels/{guild}/{channel.id}/{message_id}'),
                      allowed_mentions=NO_MENTIONS, suppress_embeds=True)
    except (discord.DiscordException, TimeoutError, OSError) as exc:
        LOG.warning('Owner not told of a refused take-back: %s', type(exc).__name__)


async def reach(fri, entry):
    """Where the reply went. Gone or unreadable, the pass tries again later, until KEEP."""
    return fri.get_channel(int(entry['channel_id'])) or await fri.fetch_channel(int(entry['channel_id']))


class TellReporter(discord.ui.DynamicItem[discord.ui.Button], template=TELL_ID):
    def __init__(self, job_id):
        self.job_id = job_id
        super().__init__(discord.ui.Button(label='Tell reporter', style=discord.ButtonStyle.secondary,
                                           custom_id=f'fri:tell:{job_id}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['job_id'])

    async def interaction_check(self, interaction):
        return tell_component(interaction, interaction.client.user.id, interaction.client.owner_id)

    async def callback(self, interaction):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        text, sent = await tell(interaction.client, self.job_id, interaction.user.id)
        if sent:
            await interaction.edit_original_response(view=None)
        await interaction.followup.send(text[:1500], ephemeral=True, allowed_mentions=NO_MENTIONS)
