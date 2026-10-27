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


class Burst(list):
    """One burst's rows, and its author's erasure baseline (see Agent.erasure) as they came in."""

    def __init__(self, since=None, alone=False):
        super().__init__()
        self.since, self.alone = since, alone


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

    def add(self, message, arrived=None, since=None, alone=False):
        """`since`: the author's erasure baseline as it came in. `alone`: a privacy control, which
        neither joins nor takes in other messages, so it's carried out and they're still answered.
        False if the message came in before an erasure the open burst came in after, so it's dropped."""
        key, now = (message.channel.id, message.author.id), time.monotonic()
        arrived = now if arrived is None else arrived
        groups, abandoned = [], []
        for rows in self.rows.get(key, []):  # Abandoned ones expire.
            (groups if now-rows[0][0] <= 60 else abandoned).append(rows)
        self.dropped([row[1] for rows in abandoned for row in rows], 'burst abandoned')
        joins = groups and arrived-groups[-1][0][0] <= BURST_SECONDS and not (alone or groups[-1].alone)
        if joins and since != groups[-1].since:
            # They erased in between, so nothing answers what came before, as forget() drops it.
            # Messages can finish coming in out of order, so that may be this one.
            if None not in (since, groups[-1].since) and all(a <= b for a, b in zip(since, groups[-1].since)):
                self.rows[key] = groups
                self.dropped([message], 'author privacy changed')
                return False
            self.dropped([row[1] for row in groups.pop()], 'author privacy changed')
            joins = False
        if not joins:
            groups.append(Burst(since, alone))
        groups[-1].append((arrived, message, message.content))
        self.rows[key] = groups
        rows = groups[-1]
        if len(rows) > MAX_MESSAGES or sum(len(row[2]) for row in rows) > MAX_TEXT:
            groups.pop()
            if not groups:
                del self.rows[key]
            self.dropped([row[1] for row in rows[:-1]], 'burst too long')  # The newest gets the notice.
            raise ValueError('Send at most five messages and 4000 characters together, then give me a moment.')
        return True

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
        """Drop a member's bursts after a privacy control, except other privacy controls."""
        for key in [key for key in self.rows if key[1] == user]:
            groups = self.rows.pop(key)
            if kept := [rows for rows in groups if rows.alone]:
                self.rows[key] = kept
            self.dropped([row[1] for rows in groups if not rows.alone for row in rows], 'author privacy changed')

    def unsend(self, channel, ids):
        """Deleted messages leave their burst, and its newest message answers for the rest. A burst
        whose newest message went is dropped whole: the rest were waiting on what was taken back.
        So is a privacy control's: deleting it may be taking it back, and an erasure can't be undone."""
        for key in [key for key in self.rows if key[0] == channel]:
            groups = []
            for rows in self.rows[key]:
                gone = [row[1] for row in rows if row[1].id in ids]
                if gone and rows[-1][1].id in ids:
                    self.dropped([row[1] for row in rows], 'message deleted')
                    continue
                if gone:
                    rows[:] = [row for row in rows if row[1].id not in ids]
                    self.dropped(gone, 'message deleted')
                groups.append(rows)
            if groups:
                self.rows[key] = groups
            else:
                del self.rows[key]


async def live_rows(agent, message, burst):
    """Refetch each source so cached/resolved messages cannot smuggle edits or authors."""
    import discord

    from . import feedback as commands

    rows, changed, unavailable = [], False, False
    # Every message is checked before refusing, so each one edited or deleted leaves the live view.
    for _, event, original in burst:
        try:
            current = await message.channel.fetch_message(event.id)
        except discord.NotFound:
            agent.withdraw(event)  # Out of the live view too, so a later turn can't show it.
            unavailable = True
            continue
        except discord.HTTPException:  # Unconfirmed: refused, but not withdrawn.
            unavailable = True
            continue
        if (current.author.id != message.author.id or current.author.bot or current.webhook_id or
                current.content != original or current.channel.id != message.channel.id):
            agent.withdraw(event)
            changed = True
            continue
        # A fetch has no server nickname; the arriving event's source matches the turn's own.
        rows.append({'source': discord_source(event).model_dump(), 'text': clean(original, MAX_TEXT)})
    if changed:
        raise ValueError('A message in that burst changed. Send a fresh request.')
    if unavailable:
        raise ValueError('A message in that burst is no longer available. Send a fresh request.')
    try:
        return checked_burst(rows, discord_source(message), clean(message.content, MAX_TEXT), fresh=True)
    except commands.UserFeedbackCheckFailure:
        raise ValueError('A message in that burst is no longer available. Send a fresh request.') from None


LINK = re.compile(r'\bhttps://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d{1,25})/(\d{1,25})/(\d{1,25})\b')
MAX_LINKS = 2


def links(message):
    """Message links the speaker pasted, outside code, in order: (guild, channel, message) ids."""
    visible = re.sub(r'```[\s\S]*?```|`[^`]*`', '', getattr(message, 'content', '') or '')
    found = []
    for match in LINK.finditer(visible):
        ids = tuple(int(part) for part in match.groups())
        if ids not in found:
            found.append(ids)
    return found[:MAX_LINKS]


async def reply_target(agent, message, posts=None, gone=None):
    """The message they replied to, and up to two they linked, when this audience may read them.

    posts, when given, collects each target's Discord message, for the images look() may inspect; gone, the IDs
    Discord said were deleted."""
    from .fri_memory import open_to_everyone

    rows = []
    reference = getattr(message, 'reference', None)
    channel = message.channel
    if reference and reference.message_id and getattr(reference, 'channel_id', channel.id) in (None, channel.id):
        if not message.guild or await readable(agent, message, channel):
            rows += await target_row(agent, channel, reference.message_id,
                                     str(message.guild.id) if message.guild else None, posts, gone)
    home = message.guild.id if message.guild else agent.fri.guild_id
    for guild_id, channel_id, message_id in links(message):
        if message_id == message.id or any(row['source']['id'] == str(message_id) for row in rows):
            continue
        linked = agent.fri.get_channel(channel_id)
        if (guild_id != home or linked is None or getattr(getattr(linked, 'guild', None), 'id', None) != home or
                not callable(getattr(linked, 'fetch_message', None))):  # A forum or a category holds no posts.
            continue
        if not in_scope(agent, linked):
            continue
        now = await fetched(linked)
        # Said in a server channel, it must be one everyone in the server can read anyway, or this one.
        if now is None or age_restricted(now) or (message.guild and linked.id != channel.id and
                                                  not open_to_everyone(now)):
            continue
        if not await readable(agent, message, linked, now):
            continue
        rows += [{**row, 'linked': True} for row in await target_row(agent, linked, message_id, str(home), posts, gone)]
    return rows


def in_scope(agent, channel):
    """A channel Fri already reads for chat or learning, not age-restricted or a private thread."""
    import discord

    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if isinstance(channel, discord.Thread) and channel.is_private():
        return False
    if parent is None or age_restricted(parent):
        return False
    return agent.allowed(channel) or str(parent.id) in agent.state.get('history_channels', [])


def age_restricted(channel):
    is_nsfw = getattr(channel, 'is_nsfw', None)
    return bool(getattr(channel, 'nsfw', False) or (callable(is_nsfw) and is_nsfw()))


async def readable(agent, message, channel, now=None):
    """The speaker, now, and Fri can both read that channel's history.

    now, from fetched(), judges it as Discord has it rather than from the cache."""
    guild = channel.guild
    if message.guild and not agent.allowed(message.channel):
        return False
    member = await agent.member(message.author.id, fresh=True, guild_id=guild.id)
    if not member:
        return False
    for who in (member, guild.me):
        permissions = (now or channel).permissions_for(who)
        if not permissions.view_channel or not permissions.read_message_history:
            return False
    return True


async def fetched(channel):
    """A linked channel (a thread's parent) with its fetched overwrites and the server's fetched roles.

    The cache can lag a revoke, and a link crosses channels (see Invitation.access). None if gone."""
    import copy

    import discord

    guild = channel.guild
    try:
        current = await guild.fetch_channel(channel.id)
        parent = await guild.fetch_channel(current.parent_id) if isinstance(current, discord.Thread) else current
        roles = await guild.fetch_roles()
    except discord.HTTPException:
        return None
    if current.id != channel.id or parent.guild.id != guild.id:
        return None
    fresh_guild = copy.copy(guild)
    fresh_guild._roles = {role.id: role for role in roles}
    parent = copy.copy(parent)
    parent.guild = fresh_guild
    return parent


async def target_row(agent, channel, message_id, guild_id, posts, gone=None):
    """One referenced message as evidence: recent, by a member who allows it, or Fri itself."""
    import discord

    from .fri_images import media
    try:
        target = await channel.fetch_message(message_id)
        source = discord_source(target)
        if (target.id != message_id or target.channel.id != channel.id or target.webhook_id or
                source.guild_id != guild_id or not 0 <= time.time()-source.timestamp().timestamp() <= REPLY_SECONDS or
                not (target.content.strip() or media(target))):
            return []
        if target.author.id != agent.fri.user.id:
            if target.author.bot:
                return []
            profile = await agent.profile(getattr(channel, 'parent_id', None) or channel.id, target.author.id)
            if not profile['enabled']:
                return []
        row = {'source': source.model_dump(), 'text': clean(target.content, 1800),
               'privacy_revision': profile['revision'] if target.author.id != agent.fri.user.id else None}
        if media(target):
            row['attachments'] = media(target)
        if posts is not None:
            posts.append(target)
        return [row]
    except discord.NotFound:
        if gone is not None:
            gone.add(message_id)
        return []
    except discord.HTTPException:
        return []


async def evidence(agent, message, burst, posts=None):
    rows = await live_rows(agent, message, burst) if len(burst) > 1 else []
    target = await reply_target(agent, message, posts)
    return {'message_burst': rows, 'reply_target': target}


async def unchanged(agent, message, context):
    """A final privacy/source fence before tools and before returning quoted evidence."""
    import discord

    unread = object()

    async def withdrawn(row, limit, current=unread):
        """Whether Discord confirms the row's message deleted or edited; if so it leaves the live view too.
        `current`: the message as this check already fetched it (None: deleted), so a failed second fetch
        can't keep a change it saw."""
        from types import SimpleNamespace
        # From its own channel: a linked post can sit in another one.
        where = str(row['source'].get('channel_id') or message.channel.id)
        channel = message.channel if where == str(message.channel.id) else agent.fri.get_channel(int(where))
        if channel is None:
            return False  # Unconfirmed: refused, but not withdrawn.
        if current is unread:
            try:
                current = await channel.fetch_message(int(row['source']['id']))
            except discord.NotFound:
                current = None
            except (discord.HTTPException, ValueError):
                return False  # Unconfirmed: refused, but not withdrawn.
        if current is None or getattr(current, 'edited_at', None) is not None or clean(current.content, limit) != row['text']:
            agent.withdraw(SimpleNamespace(id=int(row['source']['id']), channel=channel))
            return True
        return False

    try:
        unchanged, posts, gone = True, [], set()
        if context.get('reply_target') != await reply_target(agent, message, posts, gone):
            fetched = {post.id: post for post in posts}
            for row in context.get('reply_target', []):
                mid = int(row['source']['id'])
                await withdrawn(row, 1800, None if mid in gone else fetched.get(mid, unread))
            unchanged = False  # The burst still goes on, so each changed message of it is withdrawn too.
        for row in context.get('message_burst', []):  # Every one, so each changed message is withdrawn.
            try:
                current = await message.channel.fetch_message(int(row['source']['id']))
            except discord.NotFound:
                await withdrawn(row, MAX_TEXT, None)
                unchanged = False
                continue
            if (not same_source(discord_source(current), row['source']) or
                    clean(current.content, MAX_TEXT) != row['text'] or current.webhook_id):
                await withdrawn(row, MAX_TEXT, current)
                unchanged = False
        return unchanged
    except (discord.HTTPException, ValueError):
        return False
