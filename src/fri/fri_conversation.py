"""Bounded live conversation evidence. Evidence never supplies general tool authority."""

import re
import time

from pydantic import BaseModel, ConfigDict, Field

from .fri_context import ChatSource, addressed_content, discord_source, same_source
from .gene_controls import arguments, requested
from .privacy import clean

BURST_SECONDS = 5
MAX_MESSAGES = 5
MAX_TEXT = 4000
REPLY_SECONDS = 7 * 86400
PERSISTENT_MODEL_WRITES = frozenset({'work_save', 'prepare_post', 'preview_voice', 'reminder_prepare', 'plan_prepare'})


class SourceText(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: ChatSource
    text: str = Field(min_length=1, max_length=MAX_TEXT)


def checked_burst(rows, source, text, *, fresh=False):
    """Validate actual individual inputs; never manufacture one concatenated event."""
    if not rows:
        return []
    if len(rows) > MAX_MESSAGES:
        raise ValueError('Send at most five short messages together.')
    source = ChatSource.model_validate(source)
    entries = [SourceText.model_validate(row) for row in rows]
    previous = None
    for entry in entries:
        item = entry.source.check(source.guild_id or 'dm', 'private:'+source.channel_id if source.guild_id is None
                                  else 'public:'+source.channel_id, source.user_id)
        if item.kind != 'discord_message' or clean(entry.text, MAX_TEXT) != entry.text:
            raise ValueError('Burst evidence must be clean Discord message text')
        if previous is not None and int(item.id) <= previous:
            raise ValueError('Burst messages must be distinct and ordered')
        previous = int(item.id)
    if (entries[-1].source != source or entries[-1].text != text or
            sum(len(row.text) for row in entries) > MAX_TEXT or
            (entries[-1].source.timestamp()-entries[0].source.timestamp()).total_seconds() > BURST_SECONDS or
            (fresh and not -5 <= time.time()-source.timestamp().timestamp() <= 300)):
        raise ValueError('Burst evidence does not match the current message')
    return [row.model_dump() for row in entries]


def burst_continuation(origin, source, text, action, argument, revision, bot_id=''):
    """An incomplete real request plus its exact answer can use the existing handoff."""
    if not origin or action not in ('play', 'volume'):
        return None
    try:
        source = ChatSource.model_validate(source)
        rows = checked_burst([origin, {'source': source.model_dump(), 'text': text}], source, text, fresh=True)
        original = rows[0]
        visible = addressed_content(original['text'], bot_id)
        # Deliberately narrow: complete requests, capability questions and quoted commands
        # are not permission to reinterpret a later unrelated message as an argument.
        verb = r'(?:play|queue|put on|add)' if action == 'play' else r'(?:volume|set (?:the )?volume)'
        prefix = r'(?:(?:hey|yo|bro|ok|okay|fri|please|pls|plz|gene)[\s,:.!]+)*'
        request = r'(?:(?:can|could|would|will) (?:you|u) )?'
        if not requested(visible, action) or not re.fullmatch(prefix+request+verb+r'[\s?!.]*', visible, re.I):
            return None
        answer = ' '.join(addressed_content(text, bot_id).split())
        from .fri_pending import cancelled
        if cancelled(text) or answer.casefold() != ' '.join(argument.split()).casefold():
            return None
        arguments(action, argument)
        return {'goal': action, 'source': original['source'], 'request_text': original['text'],
                'revision': revision, 'burst': True}
    except (ValueError, KeyError, TypeError):
        return None


class Bursts:
    """Only hold in-flight events during the debounce, including when memory is off.

    Messages group by when they arrived, which can be well before they're processed: a member's
    messages wait while Fri's previous reply is delivered and recorded.
    """

    def __init__(self, dropped=None):
        self.rows = {}  # (channel, member): bursts, oldest first, each [(arrived, message, text)].
        self.dropped = dropped or (lambda events, reason: None)  # Told of events no reply will answer.

    def has(self, channel, user):
        groups = self.rows.get((channel, user), [])
        return bool(groups and time.monotonic()-groups[-1][0][0] <= BURST_SECONDS)

    def add(self, message, arrived=None):
        key, now = (message.channel.id, message.author.id), time.monotonic()
        arrived = now if arrived is None else arrived
        groups, abandoned = [], []
        for rows in self.rows.get(key, []):  # Abandoned ones expire.
            (groups if now-rows[0][0] <= 60 else abandoned).append(rows)
        self.dropped([row[1] for rows in abandoned for row in rows], 'burst abandoned')
        if not groups or arrived-groups[-1][0][0] > BURST_SECONDS:
            groups.append([])
        groups[-1].append((arrived, message, message.content))
        self.rows[key] = groups
        rows = groups[-1]
        if len(rows) > MAX_MESSAGES or sum(len(row[2]) for row in rows) > MAX_TEXT:
            groups.pop()
            if not groups:
                del self.rows[key]
            self.dropped([row[1] for row in rows[:-1]], 'burst too long')  # The newest gets the notice.
            raise ValueError('Send at most five messages and 4000 characters together, then give me a moment.')

    def group(self, message):
        return next((rows for rows in self.rows.get((message.channel.id, message.author.id), [])
                     if any(row[1].id == message.id for row in rows)), None)

    def latest(self, message):
        """Whether this message ends its burst; otherwise a later one answers for both."""
        rows = self.group(message)
        return bool(rows) and rows[-1][1].id == message.id

    def take(self, message):
        key, rows = (message.channel.id, message.author.id), self.group(message)
        if rows is None:
            return []
        self.rows[key].remove(rows)
        if not self.rows[key]:
            del self.rows[key]
        return rows

    def drop(self, message, reason):
        """Nothing will answer this message's burst now, so report all of it."""
        rows = self.take(message)
        self.dropped([row[1] for row in rows], reason)

    def forget(self, user):
        for key in [key for key in self.rows if key[1] == user]:
            self.dropped([row[1] for rows in self.rows.pop(key) for row in rows], 'author privacy changed')


async def live_rows(agent, message, burst):
    """Refetch each source so cached/resolved messages cannot smuggle edits or authors."""
    import discord

    from . import feedback as commands

    rows = []
    try:
        for _, event, original in burst:
            current = await message.channel.fetch_message(event.id)
            if (current.author.id != message.author.id or current.author.bot or current.webhook_id or
                    current.content != original or current.channel.id != message.channel.id):
                raise ValueError('A message in that burst changed. Send a fresh request.')
            # A fetch has no server nickname; the arriving event's source matches the turn's own.
            rows.append({'source': discord_source(event).model_dump(), 'text': clean(original, MAX_TEXT)})
        return checked_burst(rows, discord_source(message), clean(message.content, MAX_TEXT), fresh=True)
    except (discord.HTTPException, commands.UserFeedbackCheckFailure):
        raise ValueError('A message in that burst is no longer available. Send a fresh request.') from None


async def reply_target(agent, message):
    """Read only the directly referenced, currently readable message in this audience."""
    import discord

    reference = getattr(message, 'reference', None)
    if not reference or not reference.message_id:
        return []
    channel = message.channel
    if getattr(reference, 'channel_id', channel.id) not in (None, channel.id):
        return []
    if message.guild:
        if not agent.allowed(channel):
            return []
        member = await agent.member(message.author.id, fresh=True, guild_id=message.guild.id)
        if not member:
            return []
        for who in (member, message.guild.me):
            permissions = channel.permissions_for(who)
            if not permissions.view_channel or not permissions.read_message_history:
                return []
    try:
        target = await channel.fetch_message(reference.message_id)
        source = discord_source(target)
        if (target.id != reference.message_id or target.channel.id != channel.id or target.webhook_id or
                source.guild_id != (str(message.guild.id) if message.guild else None) or
                not 0 <= time.time()-source.timestamp().timestamp() <= REPLY_SECONDS or not target.content.strip()):
            return []
        if target.author.id != agent.fri.user.id:
            if target.author.bot:
                return []
            profile = await agent.profile(getattr(channel, 'parent_id', None) or channel.id, target.author.id)
            if not profile['enabled']:
                return []
        return [{'source': source.model_dump(), 'text': clean(target.content, 1800),
                 'privacy_revision': profile['revision'] if target.author.id != agent.fri.user.id else None}]
    except discord.HTTPException:
        return []


async def evidence(agent, message, burst):
    rows = await live_rows(agent, message, burst) if len(burst) > 1 else []
    target = await reply_target(agent, message)
    return {'message_burst': rows, 'reply_target': target}


async def unchanged(agent, message, context):
    """A final privacy/source fence before tools and before returning quoted evidence."""
    import discord
    try:
        if context.get('reply_target') != await reply_target(agent, message):
            return False
        for row in context.get('message_burst', []):
            current = await message.channel.fetch_message(int(row['source']['id']))
            if (not same_source(discord_source(current), row['source']) or
                    clean(current.content, MAX_TEXT) != row['text'] or current.webhook_id):
                return False
        return True
    except (discord.HTTPException, ValueError):
        return False
