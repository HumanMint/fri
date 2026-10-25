"""One live ping invites bounded evidence, never ambient collection or new tools."""

import copy
import datetime as dt
import time

import discord

from . import feedback as commands
from .fri_addressing import explicit_mention
from .fri_context import discord_source, source_identity
from .fri_errors import AuthorErased, ControllerUnavailable
from .fri_meme_media import sources
from .privacy import clean

NEARBY_SECONDS = 30 * 60
REFERENCE_SECONDS = 7 * 86400
MAX_MESSAGES = 12
CHANGED = 'That mention or its context changed while I was replying. Ask again for a fresh reply.'
# Said instead of a reply that can't go out, while the ping itself is still there to answer. Gates
# that close access (permissions, thread state, a deleted or un-pinged message) stay quiet.
CONTEXT_CHANGED = 'things changed while i was typing. ping me again and i’ll catch up.'
NOTICES = {'stale event': 'took me too long on that one. ping me again if you still need me.',
           'message changed': 'you edited that before i could answer. ping me again with the new version.',
           'nearby message changed': CONTEXT_CHANGED, 'nearby privacy changed': CONTEXT_CHANGED,
           'check failed: ControllerRejected': CONTEXT_CHANGED}
GLITCH = 'that one glitched on my end. ping me again.'
NOTICES['controller unavailable'] = GLITCH
OUTAGES = ('controller unavailable', 'check failed: ControllerUnavailable')


def notice_for(reason):
    return NOTICES.get(reason, GLITCH if reason.startswith('check failed: ') else None)


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
        self.image = None
        self.ambiguous_images = False
        self.refusal = None  # Why the last check failed, for the drop log: a gate name, never content.
        self.forgotten = agent.forgotten.get(message.author.id, 0)  # The author's erasures so far.
        self.revision = None  # The author's privacy revision as Fri first read it for this ping.

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
            await agent.withheld(message, [message], invitation.refusal, invitation)
            return None
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
            age = time.time()-discord.utils.snowflake_time(self.event.id).timestamp()
            if age > 300:
                return self.refuse('stale event')
            if age < -5:
                return self.refuse('clock skew')
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
        except (discord.HTTPException, commands.UserFeedbackCheckFailure, ValueError, AttributeError, TypeError) as exc:
            return self.refuse('check failed: '+type(exc).__name__)

    async def require_current(self):
        if not await self.current():
            raise commands.UserFeedbackCheckFailure(CHANGED)

    async def notice(self, reason):
        """What to say instead of a reply that can't go out, or None to stay quiet.

        Only while the ping still stands: Fri can still answer there, the message still pings Fri,
        and its author hasn't erased since Fri first saw it. Never any content or context.
        """
        if not (text := notice_for(reason)):
            return None
        refusal, revision = self.refusal, self.revision
        try:
            if revision is None:  # Not read yet, e.g. already stale as it came in: compare from now.
                try:
                    revision = await self.agent.revision(self.event.author.id)
                except ControllerUnavailable:
                    if reason not in OUTAGES:
                        return None
                except (commands.UserFeedbackCheckFailure, KeyError, TypeError):
                    return None
            if not await self.access():
                return None
            current = await self.event.channel.fetch_message(self.event.id)
            if (current.id != self.event.id or current.author.id != self.event.author.id or current.author.bot or
                    current.webhook_id or not explicit_mention(current, self.fri.user.id)):
                return None
            if reason == 'message changed' and current.edited_at is None:
                text = GLITCH  # Not the member's edit, so don't say it was.
            # Last, so an erasure during the checks above still counts. An outage can't be checked
            # against itself, so only a known erasure silences the notice saying so.
            erased = await self.agent.erasure(self.event.author.id, self.forgotten, revision)
            if erased and not (reason in OUTAGES and erased == 'check failed: ControllerUnavailable'):
                return None
            return text
        except (discord.HTTPException, commands.UserFeedbackCheckFailure, ValueError, AttributeError, TypeError):
            return None
        finally:
            self.refusal = refusal

    async def admit(self):
        await self.require_current()
        result = await self.fri.api('POST', '/chat/mentions/admit', {**self.scope,
            'source': self.source, 'content': self.event.content, 'bot_id': str(self.fri.user.id)})
        if result.get('allowed'):
            if self.revision is not None and result['revision'] != self.revision:
                raise AuthorErased()  # Gene fences the grant from here on, so stop at the gap before.
            self.grant = result['mention_grant']
            self.revision = result['revision']
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
            self.rows.append({'source': discord_source(item).evidence(), 'text': clean(item.content, 500),
                              'privacy_revision': profile['revision'], 'referenced': item.id == reference_id})
        self.rows.sort(key=lambda row: int(row['source']['id']))
        own = event if event.attachments else None
        referenced = self.events.get(reference_id)
        images = [m for m in self.events.values() if m.attachments]
        if own:
            self.image = own
        elif referenced and referenced.attachments:
            self.image = referenced
        elif len(images) == 1:
            self.image = images[0]
        elif len(images) > 1:
            self.ambiguous_images = True
        # Contextual images use attachments only. Never follow embeds or pasted URLs.
        if self.image and sources(self.image) != [str(a.url) for a in self.image.attachments]:
            self.image = None
        await self.require_current()

    def context(self):
        return {'invited_context': self.rows, 'ambiguous_context_images': self.ambiguous_images,
                'force_text_reply': True, 'acknowledgments_enabled': False,
                'voice_notes': self.agent.config['voice_notes']}


def force_text(result):
    """A deterministic last resort if a provider ignores the text-only schema."""
    if result.status == 'completed' and result.response_kind in ('react', 'quiet'):
        result.response_kind, result.reaction, result.allow_closing = 'reply', None, False
        result.reply = 'i’m here, but i couldn’t put a useful answer together. could you ask that another way?'
    return result
