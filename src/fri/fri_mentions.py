"""One live ping invites bounded evidence, never ambient collection or new tools."""

import copy
import datetime as dt
import time

import discord

from . import feedback as commands
from .fri_addressing import explicit_mention
from .fri_context import discord_source, source_identity
from .fri_errors import ControllerUnavailable
from .fri_images import media
from .fri_meme_media import sources
from .privacy import clean

NEARBY_SECONDS = 6 * 3600  # "The memes above" in a slow channel can be hours old; MAX_MESSAGES still bounds it.
REFERENCE_SECONDS = 7 * 86400
MAX_MESSAGES = 12
CHANGED = 'That mention or its context changed while I was replying. Ask again for a fresh reply.'


def fingerprint(message):
    return (source_identity(discord_source(message)), message.content, message.edited_at,
            tuple((str(a.url), a.content_type, a.size, a.width, a.height, a.is_spoiler())
                  for a in message.attachments))


class Invitation:
    def __init__(self, agent, message):
        self.agent, self.fri, self.event = agent, agent.fri, message
        self.source = discord_source(message).model_dump()
        self.original = fingerprint(message)
        self.outside = not agent.allowed(message.channel)
        self.scope = {'guild_id': str(message.guild.id), 'user_id': str(message.author.id),
                      'channel_id': str(message.channel.id), 'conversation_id': f'public:{message.channel.id}'}
        self.grant = None
        self.rows, self.events, self.profiles, self.snapshots = [], {}, {}, {}
        self.image = None  # The ping's own image post, or the one it replies to.
        self.images = []  # Otherwise, nearby posts with attachments, for look() to choose from.
        self.refusal = None  # Why the last check failed, for the drop log: a gate name, never content.

    @classmethod
    async def create(cls, agent, message):
        if not isinstance(message, discord.Message) or not message.guild:
            return None
        if message.author.bot or message.webhook_id or not explicit_mention(message, agent.fri.user.id):
            reason = 'ineligible event'
        elif message.guild.id != agent.fri.guild_id:
            reason = 'outside the main server'
        else:
            invitation = cls(agent, message)
            if await invitation.current(check_evidence=False):
                return invitation
            reason = invitation.refusal
        agent.miss(message, reason)
        return None

    def refuse(self, reason):
        self.refusal = reason
        return False

    async def access(self):
        """Fetch current overwrites and both members; cached channel access is insufficient."""
        event, guild = self.event, self.event.guild
        if not self.agent.config['enabled']:
            return self.refuse('fri disabled')
        if guild.id != self.fri.guild_id and not self.agent.allowed(event.channel):
            return self.refuse('outside the main server')
        channel = await guild.fetch_channel(event.channel.id)
        if channel.id != event.channel.id or channel.guild.id != guild.id:
            return self.refuse('channel changed')
        thread = isinstance(channel, discord.Thread)
        if thread:
            if channel.is_private() or channel.archived or channel.locked:
                return self.refuse('private, archived or locked thread')
            parent = await guild.fetch_channel(channel.parent_id)
        else:
            parent = channel
        if getattr(parent, 'nsfw', False):
            return self.refuse('nsfw channel')
        # Current role permissions matter as well as fresh channel overwrites.
        fresh_guild = copy.copy(guild)
        fresh_guild._roles = {role.id: role for role in await guild.fetch_roles()}
        parent = copy.copy(parent)
        parent.guild = fresh_guild
        for user in (event.author.id, self.fri.user.id):
            member = await guild.fetch_member(user)
            if member.id != user or (user == event.author.id and member.bot):
                return self.refuse('member changed')
            permissions = parent.permissions_for(member)
            if not (permissions.view_channel and permissions.read_message_history and
                    (permissions.send_messages_in_threads if thread else permissions.send_messages)):
                return self.refuse('missing permissions')
        return True

    async def current(self, *, check_evidence=True):
        try:
            if not -5 <= time.time()-discord.utils.snowflake_time(self.event.id).timestamp() <= 300:
                return self.refuse('stale event')
            if not await self.access():
                return False
            current = await self.event.channel.fetch_message(self.event.id)
            if (current.id != self.event.id or current.author.bot or current.webhook_id or
                    fingerprint(current) != self.original or not explicit_mention(current, self.fri.user.id)):
                return self.refuse('message changed')
            if check_evidence and self.grant:
                profiles = await self.profile_sources(list(self.events.values()))
                if any(profiles.get(user) != before for user, before in self.profiles.items()):
                    return self.refuse('nearby privacy changed')
                for mid, event in self.events.items():
                    current = await self.event.channel.fetch_message(mid)
                    if current.webhook_id or current.author.bot or fingerprint(current) != self.snapshots[mid]:
                        return self.refuse('nearby message changed')
            return True
        except ControllerUnavailable:
            raise  # Nothing changed; the controller is down, and the caller's outage handling says so.
        except (discord.HTTPException, commands.UserFeedbackCheckFailure, ValueError, AttributeError, TypeError) as exc:
            return self.refuse('check failed: '+type(exc).__name__)

    async def require_current(self):
        if not await self.current():
            raise commands.UserFeedbackCheckFailure(CHANGED)

    async def admit(self):
        await self.require_current()
        result = await self.fri.api('POST', '/chat/mentions/admit', {**self.scope,
            'source': self.source, 'content': self.event.content, 'bot_id': str(self.fri.user.id)})
        if result.get('allowed'):
            self.grant = result['mention_grant']
        return result

    def granted_scope(self):
        return {**self.scope, 'mention_grant': self.grant, 'source': self.source}

    async def profile_sources(self, events):
        result = await self.fri.api('POST', '/chat/mentions/profiles', {**self.granted_scope(),
            'evidence': [discord_source(event).model_dump() for event in events]})
        return result['profiles']

    async def collect(self, *, include_recent=True):
        """Read once on this invitation, retaining no channel cache or learned facts."""
        if not self.grant:
            raise commands.UserFeedbackCheckFailure('Nearby mention context needs a fresh conversation admission.')
        await self.require_current()
        event, candidates, reference_id = self.event, {}, None
        reference = event.reference
        if reference and reference.message_id and getattr(reference, 'channel_id', event.channel.id) in (None, event.channel.id):
            reference_id = reference.message_id
            try:
                candidates[reference_id] = await event.channel.fetch_message(reference_id)
            except discord.HTTPException:
                pass
        if include_recent:
            after = dt.datetime.fromtimestamp(time.time()-NEARBY_SECONDS, dt.timezone.utc)
            async for cached in event.channel.history(limit=MAX_MESSAGES, before=event, after=after, oldest_first=False):
                candidates[cached.id] = cached
        accepted = []
        for mid, item in candidates.items():
            age = (event.id-item.id)/(1 << 22)/1000
            if (item.id != mid or item.author.bot or item.webhook_id or
                    item.channel.id != event.channel.id or not item.guild or item.guild.id != event.guild.id or
                    not 0 < age <= (REFERENCE_SECONDS if mid == reference_id else NEARBY_SECONDS)):
                continue
            accepted.append(item)
        profiles = await self.profile_sources(accepted)
        for item in sorted(accepted, key=lambda m: m.id):
            user = str(item.author.id)
            profile = profiles.get(user)
            if not profile or not profile['enabled']:
                continue
            self.profiles[user] = profile
            self.events[item.id] = item
            self.snapshots[item.id] = fingerprint(item)
            self.rows = [row for row in self.rows if row['source']['id'] != str(item.id)]
            row = {'source': discord_source(item).evidence(), 'text': clean(item.content, 500),
                   'privacy_revision': profile['revision'], 'referenced': item.id == reference_id}
            if media(item):
                row['attachments'] = media(item)
            self.rows.append(row)
        self.rows.sort(key=lambda row: int(row['source']['id']))
        own = event if event.attachments else None
        referenced = self.events.get(reference_id)
        if own:
            self.image = own
        elif referenced and referenced.attachments:
            self.image = referenced
        # Without one image the ping points at, "which of these" needs all of them; look() keeps the newest that fit.
        self.images = [] if self.image else [m for m in self.events.values() if m.attachments]
        # Contextual images use attachments only. Never follow embeds or pasted URLs.
        if self.image and sources(self.image) != [str(a.url) for a in self.image.attachments]:
            self.image = None
        await self.require_current()

    def context(self):
        return {'invited_context': self.rows, 'force_text_reply': True, 'acknowledgments_enabled': False,
                'voice_notes': self.agent.config['voice_notes']}

    async def look(self, context):
        """Inspect the nearby image posts collect() found, as this invitation's evidence."""
        from .fri_images import look
        return await look(context, self.images, invitation=self)


def force_text(result):
    """A deterministic last resort if a provider ignores the text-only schema."""
    if result.status == 'completed' and result.response_kind in ('react', 'quiet'):
        result.response_kind, result.reaction, result.allow_closing = 'reply', None, False
        result.reply = 'i’m here, but i couldn’t put a useful answer together. could you ask that another way?'
    return result
