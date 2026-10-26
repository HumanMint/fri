"""Closing the loop when Gene gets fixed.

A Gene error Fri files gets a 👀, and its job is watched for three days after the error. Once that fix
ships, Fri says one dry line under the error, when the room is quiet, and never a second one for it.
Only deployed counts: a failure, a rejection, a withdrawal or a hand-off to the owner is never
announced. The owner's DM for a shipped repair that a member reported gets a Tell reporter button,
which replies to that report once.
"""

import asyncio
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
JOB = re.compile(r'[a-f0-9]{12}')
TELL_ID = re.compile(r'^fri:tell:(?P<job_id>[a-f0-9]{12})$')
SOURCE = re.compile(r'https://discord\.com/channels/(?P<guild>[0-9]{1,25}|@me)/(?P<channel>[0-9]{1,25})/(?P<message>[0-9]{1,25})')
MALFORMED = (KeyError, TypeError, ValueError, OverflowError)


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
    # The 👀 runs as its own task, waited on for MARK and never cancelled (see check): attention calls this
    # from the heartbeat, which a Discord read held up by a rate limit must not stall.
    task = asyncio.create_task(mark(agent, channel_id, message_id))
    MARKING.add(task)
    task.add_done_callback(marked)
    await asyncio.wait({task}, timeout=MARK)


async def mark(agent, channel_id, message_id):
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
    permissions = channel.permissions_for(channel.guild.me)
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
    if (await fri.api('GET', '/status')).get('active_sha') != sha:
        return 'drop'  # A rollback or a newer release came first, so "fixed" might not hold.
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


async def report(fri, job):
    """The reporter's message and where it is, checked again now: nothing has been sent yet."""
    match = SOURCE.fullmatch(job.get('source_url') or '')
    user = str(job.get('user_id', ''))
    if not match or not user.isdigit() or user in {str(fri.user.id), str(fri.agent.gene_id)}:
        raise Unreachable('there’s no member report on record to reply to')
    try:
        if match['guild'] == '@me':
            if not await fri.agent.member(int(user), fresh=True):
                raise Unreachable('they’re not in the server anymore')
            channel = await fri.fetch_channel(int(match['channel']))
            if not isinstance(channel, discord.DMChannel) or not channel.recipient or str(channel.recipient.id) != user:
                raise Unreachable('that dm isn’t theirs anymore')
        else:
            channel = fri.get_channel(int(match['channel'])) or await fri.fetch_channel(int(match['channel']))
            if (int(match['guild']) != fri.guild_id or getattr(channel, 'guild', None) is None or
                    not fri.agent.allowed(channel) or not can_send(channel)):
                raise Unreachable('i can’t post where they reported it anymore')
            if getattr(channel, 'archived', False):
                raise Unreachable('their thread’s archived, and i’m not reopening it for this')
        message = await channel.fetch_message(int(match['message']))
    except discord.NotFound:
        raise Unreachable('their report is gone') from None
    except discord.HTTPException:
        raise Unreachable('discord wouldn’t let me read their report') from None
    if str(message.author.id) != user or message.author.bot or message.webhook_id:
        raise Unreachable('that report isn’t theirs anymore')
    return channel, message


async def tell(fri, job_id, actor):
    """Claim the controller's mark, reply once, record what happened. What the owner sees, and whether it posted."""
    from .fri_workspace import send_once
    try:
        claimed = await fri.api('POST', f'/requests/{job_id}/requester-notice',
                                {'guild_id': str(fri.guild_id), 'user_id': str(actor)})
    except commands.UserFeedbackCheckFailure as exc:
        return notice_text(str(exc)), False
    job, state, message_id, sending = claimed['job'], 'failed', '', False
    try:
        channel, message = await report(fri, job)
        sending = True
        result = await send_once(fri, {'id': job_id, 'target_id': str(channel.id),
                                       'body': told_line(job['approved_sha'])}, reply_to=message.id)
        message_id, state = str(int(result['id'])), 'sent'
        text = f'told them: {message.jump_url}'
    except Unreachable as exc:
        text = f'didn’t send it: {exc}.'
    except httpx.HTTPStatusError as exc:
        # Discord refused it, so nothing posted and a later tap may try again. A 5xx may have posted.
        state = 'uncertain' if exc.response.status_code >= 500 else 'failed'
        text = (f'discord refused the reply (HTTP {exc.response.status_code}). tap again later.' if state == 'failed'
                else 'discord didn’t confirm the reply, so it might have posted. not sending it twice.')
    except (httpx.HTTPError, ValueError, KeyError, TimeoutError, OSError):
        state = 'uncertain' if sending else 'failed'
        text = ('discord didn’t confirm the reply, so it might have posted. not sending it twice.' if sending
                else 'didn’t send it. tap again later.')
    try:
        await fri.api('POST', f'/requests/{job_id}/requester-notice/finish',
                      {'lease': claimed['lease'], 'state': state, 'message_id': message_id})
    except commands.UserFeedbackCheckFailure:
        LOG.warning('Requester notice outcome for %s not recorded; it stays unretried', job_id)
    return text, state == 'sent'


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
