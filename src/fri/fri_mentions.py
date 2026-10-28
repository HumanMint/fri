"""One live ping invites bounded evidence, never ambient collection or new tools."""

import copy
import datetime as dt
import hashlib
import time

import discord

from . import feedback as commands
from .fri_addressing import explicit_mention
from .fri_context import discord_source, source_identity
from .fri_errors import AuthorErased, ControllerUnavailable
from .fri_images import details
from .fri_meme_media import gif_previews, sources, unsigned
from .fri_memes import reacted
from .privacy import clean

NEARBY_SECONDS = 6 * 3600  # "The memes above" in a slow channel can be hours old; MAX_MESSAGES still bounds it.
REFERENCE_SECONDS = 7 * 86400
MAX_MESSAGES = 12
# A direct reply to Fri's answer outside its channels reads that answer this long after it went out (NX1).
RECEIPT_SECONDS = 15 * 60
MAX_RECEIPTS = 100
MAX_EVIDENCE = 25  # Posts one privacy read can name (Gene's Profiles.evidence).
MAX_AUTHORS = 25  # Their authors, besides whoever pings (Gene fences 26 with them).
CHANGED = 'That mention or its context changed while I was replying. Ask again for a fresh reply.'
# Said instead of a reply that can't go out, while the ping itself is still there to answer. Gates
# that close access (permissions, thread state, a deleted or un-pinged message) stay quiet.
CONTEXT_CHANGED = 'things changed while i was typing. ping me again and i’ll catch up.'
NOTICES = {'stale event': 'took me too long on that one. ping me again if you still need me.',
           'message changed': 'you edited that before i could answer. ping me again with the new version.',
           'nearby message changed': CONTEXT_CHANGED, 'nearby privacy changed': CONTEXT_CHANGED,
           'image changed': CONTEXT_CHANGED,
           'check failed: ControllerRejected': CONTEXT_CHANGED}
GLITCH = 'that one glitched on my end. ping me again.'


def notice_for(reason):
    return NOTICES.get(reason, GLITCH if reason.startswith('check failed: ') else None)


def fingerprint(message):
    """A post as read, its attachments by upload: Discord renews a CDN link's signature on refetch (see unsigned)."""
    return (source_identity(discord_source(message)), message.content, message.edited_at,
            tuple((url, a.content_type, a.size, a.width, a.height, a.is_spoiler())
                  for url, a in zip(unsigned([str(a.url) for a in message.attachments]), message.attachments)))


def snapshot(message):
    """A nearby post as read: its fingerprint, and the GIF link previews look() may have inspected,
    which live in the embeds (a poster can swap or remove them without an edit time)."""
    return fingerprint(message), tuple(unsigned(gif_previews(message)))


def digest(read):
    """A post as Fri read it, holding none of its words: enough to tell it changed since."""
    return hashlib.sha256(repr(read).encode()).hexdigest()


def version(message, kind):
    """Fri's own post by its fingerprint (Fri suppresses its embeds), a member's by its snapshot (see current)."""
    return digest(fingerprint(message) if kind == 'fri' else snapshot(message))


class Receipts:
    """Fri's delivered answers to outside pings, newest last, so a direct reply to one can read it (NX1).

    In memory only: a restart forgets them, and a reply to one then reads as before. Each names the posts and
    authors its answer drew on, as Fri read them, never their words. One whose answer, or any post it drew on,
    is deleted or edited, or that drew on a member who used a privacy control, goes at once."""

    def __init__(self):
        self.entries = {}  # Fri's message ID -> its receipt (see Invitation.answered).

    def keep(self, mid, receipt):
        self.entries.pop(mid, None)
        self.entries[mid] = receipt
        now = time.monotonic()
        for old in [old for old, entry in self.entries.items() if entry['until'] <= now]:
            del self.entries[old]
        while len(self.entries) > MAX_RECEIPTS:
            del self.entries[next(iter(self.entries))]

    def get(self, mid):
        receipt = self.entries.get(mid)
        if receipt is not None and receipt['until'] <= time.monotonic():
            del self.entries[mid]
            return None
        return receipt

    def withdraw(self, ids):
        ids = {int(i) for i in ids if str(i).isdigit()}
        for mid in [mid for mid, entry in self.entries.items() if mid in ids or ids & entry['sources'].keys()]:
            del self.entries[mid]

    def forget(self, user):
        for mid in [mid for mid, entry in self.entries.items() if str(user) in entry['authors']]:
            del self.entries[mid]


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
        self.image = None  # The ping's own image post, the one it replies to, or their photo just before (see current).
        self.images = []  # Otherwise, nearby posts with attachments, for look() to choose from.
        self.refusal = None  # Why the last check failed, for the drop log: a gate name, never content.
        self.forgotten = agent.forgotten.get(message.author.id, 0)  # The author's erasures so far.
        self.revision = None  # The author's privacy revision as Fri first read it for this ping.
        self.fetched = None  # Its channel (a thread's parent) as the last access check read it (see access).
        self.checked = 0  # Full checks passed, so a later check can tell one just ran (see Fri.chat_once).
        self.latest = message  # The ping as the last check refetched it, its embeds as they are by then.
        # Outside, Fri's own answer this ping replies to, and every post that drew on, Fri's or a member's:
        # message ID -> (message, kind, version), checked with the nearby posts but never shown (see answer).
        self.carried = {}
        self.inherited = None  # That answer's receipt, so one for this ping's answer keeps its ancestry.
        self.answer_id = None  # That answer's message ID, while this ping reads it.
        self.out = None  # When Fri's answer to this ping went out (time.monotonic()), once it did.

    @property
    def image(self):
        return self._image

    @image.setter
    def image(self, message):
        self._image, self.image_snapshot = message, fingerprint(message) if message is not None else None

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

    async def access(self, *, control=False):
        """Fetch current overwrites and both members; cached channel access is insufficient. A privacy
        `control` works with Fri paused."""
        event, guild = self.event, self.event.guild
        if not control and not self.agent.config['enabled']:
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
        self.fetched = parent  # Whether everyone can read it is judged from this read too (see Fri.talk).
        for user in (event.author.id, self.fri.user.id):
            member = await guild.fetch_member(user)
            if member.id != user or (user == event.author.id and member.bot):
                return self.refuse('member changed')
            permissions = parent.permissions_for(member)
            if not (permissions.view_channel and permissions.read_message_history and
                    (permissions.send_messages_in_threads if thread else permissions.send_messages)):
                return self.refuse('missing permissions')
        return True

    def unchanged(self, current):
        """The ping as Discord has it now is still the one that came in, and still pings Fri."""
        return (current.id == self.event.id and not current.author.bot and not current.webhook_id and
                fingerprint(current) == self.original and explicit_mention(current, self.fri.user.id))

    async def ping_unchanged(self):
        """The ping refetched is unchanged (see unchanged). Edited or deleted, it leaves the live view too, for
        later turns; a delete still raises NotFound for the caller."""
        try:
            current = await self.event.channel.fetch_message(self.event.id)
        except discord.NotFound:
            self.agent.withdraw(self.event)
            raise
        if not self.unchanged(current):
            self.agent.withdraw(self.event)
            return False
        self.latest = current
        return True

    async def current(self, *, check_evidence=True, control=False):
        try:
            age = time.time()-discord.utils.snowflake_time(self.event.id).timestamp()
            if age > 300:
                return self.refuse('stale event')
            if age < -5:
                return self.refuse('clock skew')
            # First, the image post it inspects when that's neither the ping nor a nearby message (e.g. their
            # photo just before the ping), so the reads below, the ping's among them, come after it.
            image = self.image
            if check_evidence and image is not None and image.id != self.event.id and image.id not in self.events:
                if fingerprint(await self.event.channel.fetch_message(image.id)) != self.image_snapshot:
                    return self.refuse('image changed')
            if not await self.access(control=control):
                return False
            if not await self.ping_unchanged():
                return self.refuse('message changed')
            if check_evidence and self.grant:
                profiles = await self.profile_sources(self.evidence())
                if any(profiles.get(user) != before for user, before in self.profiles.items()):
                    return self.refuse('nearby privacy changed')
                changed = False  # Every one is checked, so each edited or deleted leaves the live view.
                for mid, event in self.events.items():
                    try:
                        current = await self.event.channel.fetch_message(mid)
                    except discord.NotFound:
                        self.agent.withdraw(event)
                        changed = True
                        continue
                    if current.webhook_id or current.author.bot or snapshot(current) != self.snapshots[mid]:
                        self.agent.withdraw(event)
                        changed = True
                for mid, (event, kind, before) in self.carried.items():  # And what Fri's answer drew on.
                    if mid in self.events:
                        continue  # Read nearby too, just above, as the same version (see answer).
                    try:
                        current = await self.event.channel.fetch_message(mid)
                    except discord.NotFound:
                        current = None
                    if current is None or not self.same(current, mid, kind, before):
                        self.agent.withdraw(event)
                        changed = True
                if changed:
                    return self.refuse('nearby message changed')
                # The ping again, as an edit during the privacy read would otherwise go unseen.
                if not await self.ping_unchanged():
                    return self.refuse('message changed')
                # Their privacy once more, a controller read, so an opt-out during the refetches above counts too.
                profiles = await self.profile_sources(self.evidence())
                if any(profiles.get(user) != before for user, before in self.profiles.items()):
                    return self.refuse('nearby privacy changed')
            if not self.standing():  # After the last wait: a withdrawal during the reads above counts too.
                return self.refuse('nearby message changed')
            self.checked += check_evidence
            return True
        except ControllerUnavailable:
            raise  # Nothing changed; the controller is down, and the caller's outage handling says so.
        except (discord.HTTPException, commands.UserFeedbackCheckFailure, ValueError, AttributeError, TypeError) as exc:
            return self.refuse('check failed: '+type(exc).__name__)

    async def require_current(self):
        if not await self.current():
            raise commands.UserFeedbackCheckFailure(CHANGED)

    async def notice(self, reason, *, fenced=True, control=False):
        """What to say instead of a reply that can't go out, or None to stay quiet.

        Only while the ping still stands: Fri can still answer there, the message still pings Fri,
        and, if `fenced`, its author hasn't erased since Fri first saw it. A privacy control isn't:
        it always hears how it went, whatever it or another one changed, and with Fri paused, as it works
        then (see access). Never any content or context.
        """
        if not (text := notice_for(reason)):
            return None
        refusal, revision = self.refusal, self.revision
        try:
            if fenced and revision is None:  # Not read yet, e.g. already stale as it came in: compare from now.
                try:
                    revision = await self.agent.revision(self.event.author.id)
                except (commands.UserFeedbackCheckFailure, KeyError, TypeError):
                    return None
            if not await self.access(control=control):
                return None
            current = await self.event.channel.fetch_message(self.event.id)
            if (current.id != self.event.id or current.author.id != self.event.author.id or current.author.bot or
                    current.webhook_id or not explicit_mention(current, self.fri.user.id)):
                return None
            if reason == 'message changed' and fingerprint(current) == self.original:
                text = CONTEXT_CHANGED  # Another message in its burst changed, not this one.
            elif reason == 'message changed' and current.edited_at is None:
                text = GLITCH  # Not the member's edit, so don't say it was.
            # Last, so an erasure during the checks above still counts.
            if fenced and await self.agent.erasure(self.event.author.id, self.forgotten, revision):
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

    def standing(self):
        """Fri's earlier answer this ping reads, if any, may still be read, as of now: its receipt is the one Fri
        read, still kept (a delete or edit of the answer or a post it drew on, or a privacy control of an author,
        drops it at once, see Receipts) and in date, and nothing it carries was marked taken back since. No waits,
        so it holds up to the model request or the send that runs it last."""
        if self.inherited is None:
            return True
        return (self.agent.receipts.entries.get(self.answer_id) is self.inherited and
                self.inherited['until'] > time.monotonic() and
                not any(mid in self.agent.unsent or str(mid) in self.agent.withdrawn for mid in self.carried))

    def evidence(self):
        """Every member's post this ping reads or its answer draws on, once each, for their authors' privacy."""
        drawn = {mid: event for mid, (event, kind, _) in self.carried.items() if kind != 'fri'}
        return list({**drawn, **self.events}.values())

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
        answer = await self.answer(candidates.get(reference_id), accepted) if self.outside and reference_id else None
        nearby = {item.id for item in accepted}
        drawn = [post for mid, (post, kind, _) in (answer[1] if answer else {}).items()
                 if kind != 'fri' and mid not in nearby]
        authors = {str(item.author.id) for item in accepted+drawn} | {str(event.author.id)}
        if len(accepted+drawn) > MAX_EVIDENCE or len(authors) > MAX_AUTHORS+1:
            answer, drawn = None, []  # More than one privacy read can carry: it's read as before, never cut short.
        profiles = await self.profile_sources(accepted+drawn)
        notes, stale = self.agent.ensure_memes().notes(), []
        for item in sorted(accepted, key=lambda m: m.id):
            user = str(item.author.id)
            profile = profiles.get(user)
            if not profile or not profile['enabled']:
                continue
            self.profiles[user] = profile
            self.events[item.id] = item
            self.snapshots[item.id] = snapshot(item)
            self.rows = [row for row in self.rows if row['source']['id'] != str(item.id)]
            self.rows.append({'source': discord_source(item).evidence(), 'text': clean(item.content, 500),
                              'privacy_revision': profile['revision'], 'referenced': item.id == reference_id,
                              **details(item), **reacted(notes, item, profile, stale)})
        self.agent.ensure_memes().drop(stale)
        if answer:
            receipt, carried = answer
            if all((profile := profiles.get(user)) and profile['enabled'] and profile['revision'] == revision
                   for user, revision in receipt['authors'].items()):
                item = carried[reference_id][0]
                for user in receipt['authors']:
                    self.profiles[user] = profiles[user]  # Checked again with the nearby authors (see current).
                self.carried.update(carried)
                self.inherited, self.answer_id = receipt, reference_id
                self.agent.saw(list(carried))  # So a delete while Fri replies is noticed (see Agent.unsend).
                self.rows = [row for row in self.rows if row['source']['id'] != str(item.id)]
                self.rows.append({'source': discord_source(item).evidence(), 'text': clean(item.content, 1800),
                                  'referenced': True, 'fri': True, **details(item)})
            else:  # Someone it drew on changed their privacy since: it never counts again.
                self.agent.receipts.withdraw([reference_id])
        self.rows.sort(key=lambda row: int(row['source']['id']))
        own = event if event.attachments else None
        referenced = self.events.get(reference_id)
        if own:
            self.image = own
        elif referenced and referenced.attachments:
            self.image = referenced
        # Without one image the ping points at, "which of these" needs all of them; look() keeps the newest that fit.
        self.images = [] if self.image else [m for m in self.events.values() if m.attachments or gif_previews(m)]
        # Contextual images use attachments, or a GIF link's Discord-made preview. Never other embeds or URLs.
        if self.image and sources(self.image) != [str(a.url) for a in self.image.attachments]:
            self.image = None
        await self.require_current()

    async def answer(self, item, accepted):
        """Fri's own outside answer this ping replies to, with its receipt (see answered), while that still
        stands: in this channel, as Fri posted it, and every post it drew on still there as Fri read it. Their
        authors' privacy is read next, with the nearby posts' (see collect). None on any doubt: it's read as before."""
        receipts = self.agent.receipts
        receipt = receipts.get(item.id) if item is not None else None
        if (receipt is None or receipt['channel'] != self.event.channel.id or not item.guild or
                item.guild.id != self.event.guild.id or not item.id < self.event.id or
                not self.same(item, item.id, 'fri', receipt['version'])):
            return None
        nearby, carried = {m.id: m for m in accepted}, {item.id: (item, 'fri', receipt['version'])}
        for mid, (kind, before) in receipt['sources'].items():
            # Gene reads an author's privacy for a post up to a week old; this ping's checks run for 300s more.
            if (not 0 < (self.event.id-mid)/(1 << 22)/1000 <= REFERENCE_SECONDS-300 or
                    str(mid) in self.agent.withdrawn or mid in self.agent.unsent):
                return None
            try:
                current = nearby.get(mid) or await self.event.channel.fetch_message(mid)
            except discord.NotFound:
                receipts.withdraw([mid])
                return None
            except discord.HTTPException:
                return None
            if not self.same(current, mid, kind, before):
                receipts.withdraw([mid])
                return None
            carried[mid] = (current, kind, before)
        return receipt, carried

    def same(self, current, mid, kind, before):
        """A post as Discord has it now is the one a receipt names, as Fri read it: Fri's own or a member's."""
        return (current.id == mid and not current.webhook_id and current.channel.id == self.event.channel.id and
                (current.author.id == self.fri.user.id if kind == 'fri' else not current.author.bot) and
                version(current, kind) == before)

    def delivered(self):
        self.out = time.monotonic()

    def answered(self, sent):
        """`sent`, Fri's answer to this outside ping, went out and was saved: keep its receipt, so a direct reply
        to it here reads it (see answer). Its sources are every post this ping read, the ping too, and what an
        answer it read drew on in turn; its authors, everyone who wrote them, at the privacy revision Fri read.
        It lasts 15 minutes from when it went out (see delivered), never past the answer it read. Kept only if one
        later privacy read can carry it all."""
        try:
            if not (self.outside and self.grant and self.revision is not None and self.out is not None and
                    isinstance(sent, discord.Message) and
                    sent.channel.id == self.event.channel.id and sent.author.id == self.fri.user.id):
                return
            if self.image is not None and self.image.id != self.event.id and self.image.id not in self.events:
                return  # A post it looked at that no receipt would name.
            inherited = self.inherited or {'sources': {}, 'authors': {}, 'until': float('inf')}
            sources = {**inherited['sources'], **{mid: (kind, before) for mid, (_, kind, before) in self.carried.items()}}
            reads = [(self.event.id, ('post', version(self.latest, 'post'))),
                     *((mid, ('post', digest(self.snapshots[mid]))) for mid in self.events)]
            for mid, entry in reads:
                if sources.setdefault(mid, entry) != entry:
                    return  # Two reads of one post that differ: it changed while Fri read it.
            authors = dict(inherited['authors'])
            for user, revision in [(str(self.event.author.id), self.revision),
                                   *((user, profile['revision']) for user, profile in self.profiles.items())]:
                if authors.setdefault(user, revision) != revision:
                    return
            if (sum(kind != 'fri' for kind, _ in sources.values()) > MAX_EVIDENCE or len(authors) > MAX_AUTHORS or
                    any(mid in self.agent.unsent or str(mid) in self.agent.withdrawn for mid in [*sources, sent.id]) or
                    not self.standing()):
                return
            self.agent.receipts.keep(sent.id, {
                'channel': self.event.channel.id, 'version': version(sent, 'fri'), 'sources': sources,
                'authors': authors, 'until': min(self.out+RECEIPT_SECONDS, inherited['until'])})
        except (AttributeError, KeyError, TypeError, ValueError):
            return  # Delivered all the same; a reply to it reads as before.

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
