"""Fri's separate Discord identity; trusted UI, bounded chat, one controller authority."""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from . import feedback as commands
from .fri_actions import Actions
from .fri_addressing import addressed_text
from .fri_chat import Chat, ChatResult
from .fri_context import discord_source
from .fri_errors import (
    UNREACHABLE,
    AuthorErased,
    Continued,
    ControllerRejected,
    ControllerUnavailable,
    GateClosed,
)
from .fri_images import CHANGED as IMAGES_CHANGED
from .fri_images import unchanged as images_unchanged
from .fri_memory import (
    GENERATIONS,
    PROFILES,
    SUMMARIES,
    describe_memory,
    describe_profile,
    drop_unheard,
    memory_intent,
    note_place,
    place_notes,
    profiled,
    server_wide,
)
from .fri_notices import Busy, ReplyInProgress, WaitingNotices, notice_text
from .fri_panel import DraftView, HomeView, authorized, card, reply, sessions
from .fri_protocol import PROTOCOL, compatible
from .fri_reports import ReportButton, report_component
from .fri_requests import request_dm_component
from .fri_responses import quick_closure
from .fri_reviews import ReviewButton, notify_owner
from .fri_usage import describe_usage, usage_query, usage_requested, window_applied
from .panels import defer_panel
from .privacy import clean
from .reviews import owner_dm_component
from .subscription import Subscription, private_json

LOG = logging.getLogger('fri')
NO_MENTIONS = discord.AllowedMentions.none()
SIGN_IN_NOTICE = ('heads up: my chatgpt sign-in stopped working, so every reply fails until you reconnect me. '
                  'steps are in fri’s docs/operations.md under “New installations”.')
FIRST_MEMORY = ('\n\n-# i kept a small note from this, something you like or are working on. ask “what do you '
                'remember about me?” or say “forget me” any time.')
FIRST_PROFILE = ('\n\n-# i kept a small note from this, and it follows you around the server. what you’re working '
                 'on stays where you said it. ask “what do you remember about me?” or say “forget me” any time.')
FIRST_HERE = ('\n\n-# i kept a small note from this, something you like or are working on, just for this chat. '
              'ask “what do you remember about me?” or say “forget me” any time.')
MAX_PROVENANCE = 200
ERROR_BODY = 5  # Seconds for a refusal's small body, well inside the watch's 10 s read.
CONTRACT_RETRY = 10  # Seconds between contract reads while the controller is down at startup.
HEARD_SECONDS = 1800  # Longer than any message waits for its reply; ambient attention looks back 15 minutes.
UNATTRIBUTED = ('Earlier reply-context check. Fetch current Discord evidence for follow-up details; '
                'no quoted findings are retained here.')
UNAVAILABLE = '[an earlier fri reply whose sources are no longer available]'
LINKED = ('Earlier answer about a post linked from another channel. Open the link again for details; '
          'nothing from it is retained here.')
SUMMARIZED = 'Earlier answer drawing on what they talked about in other channels. Nothing from it is retained here.'
SUMMARIZED_QUESTION = 'an earlier question drawing on other channels; nothing from it is kept here. ask what they meant.'
LEGACY = '[an earlier fri reply from before fri tracked its sources]'
CHANGED = 'someone in that context changed their privacy settings while i was replying. ask again for a fresh answer.'
CONVERSATION_CHANGED = 'the conversation changed while i was typing. ask again and i’ll catch up.'
READ_CHANGED = 'that chat changed while i was reading it. ask again.'
WITHDRAWN = 'the message i was answering got edited or deleted while i was on it. ask again if you still want it.'
CLOSED = 'a channel i was pulling from just went private while i was replying, so that stays unsaid. ask again.'
# Reads of other channels, archives, learning reports or diagnostics stay neutral breadcrumbs.
PRIVATE_READS = frozenset({'read_recent', 'history_search', 'learning_overview', 'run_learning', 'gene_diagnose'})


def linked_elsewhere(context, source):
    """The answer drew on a post linked from another channel. Its saved text couldn't carry that
    channel's access, so it stays a neutral breadcrumb, like a read of another channel."""
    return any(row.get('linked') and row['source'].get('channel_id') != source.channel_id
               for row in context.get('reply_target', []))


def summarized_elsewhere(context):
    """The public answer had Fri's summary of the member from another channel in view. Its saved text and
    its copy in the live view couldn't carry that channel's access, so it stays a neutral breadcrumb, like a
    read of another channel. In the member's private chat, as for their notes, only they ever see it."""
    fence = context.get('fence') or {}
    here = (fence.get('turn') or {}).get('conversation_id', '')
    return here.startswith('public:') and any(origin != here for origin in fence.get('summarized', ()))


def audience(context):
    """Where the turn's words may follow the member (see Fri.talk): no further than their channel, once a check
    before a model request read it from Discord keeping anyone out, whatever the cache said (fri #97)."""
    return 'conversation' if (context.get('fence') or {}).get('restricted') else context.get('audience')


async def noop(stage):
    pass


def private_read(receipts):
    """Includes a diagnosis nested in an uncertain Gene command receipt."""
    return any(r.get('kind') in PRIVATE_READS or r.get('diagnostic') or (r.get('gene_result') or {}).get('diagnostic')
               for r in receipts)


def attributed(context):
    """Withhold what was saved without provenance (before this release, or by a legacy companion).

    It may quote other members with no record of whom, so nothing may inherit it as complete.
    """
    context['turns'] = [turn if turn.get('attributed') or not turn.get('reply')
                        else {**turn, 'reply': LEGACY, 'provenance': []} for turn in context.get('turns', [])]
    if context.get('pending') and 'provenance' not in context['pending']:
        context['pending'] = None
    return context


def turn_provenance(context, speaker, actions=None):
    """Other members whose messages shaped this turn, with the privacy revision observed.

    Every context source carrying another member's words contributes: the live channel view
    (including Fri's own replies), saved history, notes, a pending clarification, the channel summary,
    reply targets (Fri's resolved to their sources), nearby invited context and read evidence.
    """
    seen = {}
    def add(user, revision):
        # Two revisions for one member means their privacy changed while this turn was read.
        # Never keep either label: the older read would be relabelled as current.
        if str(user) != str(speaker) and seen.setdefault(str(user), int(revision)) != int(revision):
            raise commands.UserFeedbackCheckFailure(CHANGED)
    inherited = [item for turn in context.get('turns', []) for item in turn.get('provenance', [])]
    inherited += (context.get('pending') or {}).get('provenance', [])
    inherited += context.get('summary_provenance', []) + context.get('reference_provenance', [])
    # A member's words from another channel can repeat someone else's: inherit what they drew on.
    inherited += [item for row in context.get('elsewhere', []) for item in row.get('provenance', [])]
    # So can a note: it was learned while other members' words were in view.
    inherited += [item for note in (context.get('personal_memory') or {}).get('notes', [])
                  for item in note.get('provenance', [])]
    # And Fri's own summary of the member, built from their words, which can repeat someone else's.
    inherited += (context.get('member_summary') or {}).get('provenance', [])
    for item in inherited:
        add(item['user_id'], item['revision'])
    for row in context.get('recent', []):
        if row.get('gene_error'):
            continue
        if row.get('fri'):  # Fri's own earlier reply carries the members it was built from.
            for item in row.get('provenance', []):
                add(item['user_id'], item['revision'])
        else:
            add(row['user_id'], row.get('revision', 0))
    for row in context.get('reply_target', []) + context.get('invited_context', []):
        if row.get('privacy_revision') is not None:
            add(row['source']['user_id'], row['privacy_revision'])
    for (_, user), revision in (getattr(actions, 'revisions', None) or {}).items():
        add(user, revision)
    return [{'user_id': user, 'revision': revision} for user, revision in sorted(seen.items())]


class FriTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        return await authorized(self.client, interaction)

    async def on_error(self, interaction, error):
        LOG.warning('Fri command failed: %s', type(error).__name__)
        await reply(self.client, interaction, 'That action did not complete. Please try again.')


async def patiently(fri, read):
    """A controller read Fri needs before joining the gateway, waited out while the controller is down.

    Discord's HTTP API already works here, so the watch still tells the owner when the controller is
    what's down. A rejected or incompatible controller, or a page that isn't the controller's, still
    stops startup at once: a wrong URL or a proxy's page isn't an outage that waiting fixes."""
    while True:
        try:
            return await read()
        except ControllerUnavailable as exc:
            if exc.answered:
                raise
            if fri.watcher.due():
                LOG.warning('Controller unreachable at startup; waiting for it')
                await fri.watcher.check()
            await asyncio.sleep(CONTRACT_RETRY)


def persistent_buttons():
    """Buttons found again by their custom ID after a timeout or restart, plus the fallback that says a
    draft's, preview's or memory panel's view ended (fri_buttons.Ended)."""
    from .fri_buttons import Ended
    from .fri_fixes import TellReporter
    from .fri_memory_panel import MemoryOpen
    from .fri_social import SocialButton
    from .fri_workspace import PostButton
    return ReviewButton, ReportButton, PostButton, SocialButton, TellReporter, MemoryOpen, Ended


class Fri(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.dm_messages = True
        intents.message_content = True
        visibility = discord.Status.online if os.getenv('FRI_PRESENCE') == 'online' else discord.Status.invisible
        super().__init__(intents=intents, status=visibility,
                         allowed_mentions=NO_MENTIONS, max_messages=None)
        self.bot = self
        self.guild_id = int(os.environ['GENE_GUILD_ID'])
        self.owner_id = int(os.environ['GENE_OWNER_ID'])
        self.workshop_id = int(os.environ['GENE_WORKSHOP_CHANNEL_ID'])
        self.url = os.getenv('GENE_CONTROLLER_URL', 'http://controller:8787').rstrip('/')
        self.token = os.environ['GENE_FRI_TOKEN']
        self.data = Path(os.getenv('FRI_DATA', '/data'))
        self.chat = Chat(Subscription(os.getenv('FRI_CHATGPT_AUTH', '/auth/session.json')))
        self.tree = FriTree(self)
        self.busy = Busy()
        self.waiting_notices = WaitingNotices(self.busy)
        self.usage_lock = asyncio.Lock()
        self.chat_slots = asyncio.Semaphore(2)
        self.noticed_refusal = None
        self.sign_in_notice = None
        self.session = None
        self.agent = None
        self.capabilities = frozenset()
        from .fri_watch import Watch
        self.watcher = Watch(self)
        self.watching = None
        self.heard = {}  # Message id: (when Fri first saw it, whether everyone could read its channel then).

    @property
    def profiles(self):
        return PROFILES in self.capabilities

    @property
    def summaries(self):
        return SUMMARIES in self.capabilities and self.profiles

    def hear(self, message):
        """Whether everyone could read this message where it was sent, decided once, when Fri first sees it.

        A channel opened while Fri waits or writes its reply can't widen what was said while it was closed.
        Decisions outlast any wait for a reply; a message first seen later than that counts as restricted.
        """
        now = time.monotonic()
        while self.heard and now-next(iter(self.heard.values()))[0] > HEARD_SECONDS:
            del self.heard[next(iter(self.heard))]
        if message.id not in self.heard:
            age = time.time()-discord.utils.snowflake_time(message.id).timestamp()
            self.heard[message.id] = (now, age < HEARD_SECONDS and server_wide(message.channel))
        return self.heard[message.id][1]

    def first_memory(self, saved):
        """Where the first kept note will show, once the controller keeps member profiles."""
        if not self.profiles:
            return FIRST_MEMORY
        return {'server': FIRST_PROFILE, 'conversation': FIRST_HERE}.get(saved.get('memory_scope'), FIRST_MEMORY)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        contract = await patiently(self, lambda: self.api('GET', '/fri/contract'))
        if not compatible(contract):
            raise RuntimeError('unsupported gene controller contract; fri did not start')
        self.capabilities = frozenset(contract['capabilities'])
        from .fri_agent import Agent
        self.agent = Agent(self)
        await patiently(self, self.agent.start)
        self.add_view(HomeView(self))
        self.add_dynamic_items(*persistent_buttons())

        @self.tree.command(name='fri', description='Chat, request a feature or fix, and follow development.',
                           guild=discord.Object(id=self.guild_id))
        async def menu(interaction: discord.Interaction):
            await reply(self, interaction, embed=card('Fri', 'Say “fri”, @mention me, or reply to one of my messages '
                'in this workshop. This panel is here for private chat and settings. '
                'I can keep small preferences you share; ask what I remember or tell me to forget you.'),
                view=HomeView(self, interaction.user.id))

        await self.tree.sync(guild=discord.Object(id=self.guild_id))
        self.health.start()
        self.owner_reviews.start()
        self.watching = asyncio.create_task(self.keep_watch())

    def dispatch(self, event, /, *args, **kwargs):
        if event == 'interaction' and args:
            interaction = args[0]
            channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
            allowed = interaction.guild_id == self.guild_id and (channel == self.workshop_id or
                (self.agent and self.agent.allowed(interaction.channel)))
            behavior = (interaction.user.id == self.owner_id and
                str((interaction.data or {}).get('custom_id', '')).startswith('fri:behavior:') and
                getattr(getattr(interaction, 'message', None), 'author', None) and
                interaction.message.author.id == getattr(self.user, 'id', None))
            if behavior:
                allowed = True
            from .fri_memory_panel import memory_component
            from .fri_workspace import post_component
            if post_component(interaction, getattr(self.user, 'id', None), self.owner_id):
                allowed = True
            if memory_component(interaction, getattr(self.user, 'id', None)):
                allowed = True
            from .fri_social import component as social_component
            if social_component(interaction, getattr(self.user, 'id', None)):
                allowed = True
            from .fri_fixes import tell_component
            if (not allowed and not owner_dm_component(interaction, getattr(self.user, 'id', None), self.owner_id)
                    and not tell_component(interaction, getattr(self.user, 'id', None), self.owner_id)
                    and not report_component(interaction, getattr(self.user, 'id', None), self.owner_id)
                    and not request_dm_component(interaction, getattr(self.user, 'id', None))):
                return
        super().dispatch(event, *args, **kwargs)

    async def is_owner(self, user):
        return user.id == self.owner_id

    async def api(self, method, path, payload=None):
        try:
            async with self.session.request(method, self.url + path, json=payload,
                    headers={'Authorization': 'Bearer ' + self.token, 'X-Fri-Contract': PROTOCOL}) as response:
                foreign = False  # A reply that isn't JSON or was cut off may not be the controller's: a proxy's page, say.
                try:
                    async with asyncio.timeout(ERROR_BODY if response.status >= 400 else None):
                        data = await response.json()
                except Exception as exc:  # Not JSON, or cut off or stalled partway: a refusal is still a refusal.
                    if response.status < 400:
                        if isinstance(exc, aiohttp.ContentTypeError):  # Someone else's page, sent as a success.
                            raise ControllerUnavailable(UNREACHABLE, answered=True) from None
                        raise
                    data, foreign = None, not isinstance(exc, (ValueError, RecursionError))  # A refusal keeps its status when its JSON can't be read.
                detail = str(data.get('detail', 'Controller rejected this request')) if isinstance(data, dict) else (
                    'Controller rejected this request')
                if 400 <= response.status < 500:
                    # Still a refusal, so startup stops; one that isn't the controller's has no status to act on.
                    raise ControllerRejected(detail, None if foreign else response.status)
                if response.status >= 400:  # Its detail is a controller fault, for the log, not for members.
                    LOG.warning('Controller %s %s failed: HTTP %s', method, path.split('?')[0], response.status)
                    raise ControllerUnavailable(UNREACHABLE)
                return data
        except (aiohttp.ClientError, TimeoutError):
            raise ControllerUnavailable(UNREACHABLE) from None

    async def flush_usage(self):
        async with self.usage_lock:
            for path in sorted((self.data / 'usage').glob('*.json')):
                try:
                    report = json.loads(path.read_text())
                except FileNotFoundError:
                    continue  # An isolated evaluation process may have delivered this receipt.
                except (ValueError, RecursionError):  # Deeply nested JSON raises RecursionError.
                    self.set_aside(path, 'unreadable')
                    continue
                try:
                    await self.api('POST', '/chat/usage/report', report)
                except ControllerRejected as exc:
                    if exc.status not in (400, 422):
                        raise  # Credentials or contract: every receipt would fail alike, so all stay queued.
                    self.set_aside(path, f'HTTP {exc.status}')
                    continue
                path.unlink(missing_ok=True)

    def set_aside(self, path, cause):
        """A receipt the controller refuses can never succeed; keep it for inspection without blocking later ones."""
        rejected = path.parent / 'rejected'
        try:
            rejected.mkdir(mode=0o700, exist_ok=True)
            path.replace(rejected / path.name)
        except FileNotFoundError:
            return
        except OSError as exc:  # It stays queued and is offered again, after the receipts behind it.
            LOG.warning('Usage receipt refused (%s) and could not be set aside: %s', cause, type(exc).__name__)
            return
        LOG.warning('Usage receipt set aside in usage/rejected: %s', cause)

    async def chat_once(self, text, context):
        async def ready():  # Signed in by now, so nothing waits between these checks and the request they clear.
            fence = context.get('fence')
            looked = [read['event'].id for read in context.get('image_reads', [])]
            if context.get('image_event') is not None:
                looked.append(context['image_event'].id)
            # Every message this request carries: the live view's rows, what it quotes or replies to, the turn's
            # own burst, the posts it looked at and the posts a read showed. One place, so a delete of any of
            # them is marked even once it leaves the live view (see unsend), and delivery refuses after it.
            quoted = [int(row['source']['id']) for row in context.get('reply_target', []) + context.get('invited_context', [])
                      if str(row.get('source', {}).get('id', '')).isdigit()]
            read = [mid for entry in getattr((fence or {}).get('actions'), 'reads', ()) for mid in entry['shown']]
            carried = [*looked, *quoted, *read, *(int(row['id']) for row in context.get('recent', [])
                                                  if not row.get('gene_error') and str(row.get('id', '')).isdigit())]
            # Fri's own among them, its cached replies and a reply target it wrote: an update with no author for one
            # is no member's edit, even once it's out of the live view (see Agent.edit).
            me = str(getattr(self.user, 'id', ''))
            ours = [int(row['id']) for row in context.get('recent', [])
                    if row.get('fri') and str(row.get('id', '')).isdigit()]
            ours += [int(row['source']['id']) for row in context.get('reply_target', []) + context.get('invited_context', [])
                     if me and row.get('source', {}).get('user_id') == me and str(row['source'].get('id', '')).isdigit()]
            if self.agent:  # Before the checks below refetch them: a delete after that is marked (see unsend).
                self.agent.saw(carried, ours)
            # The delivery check, run before the request too: the words of anyone in view who erased or
            # changed their privacy since they were read never reach the model. An outside @mention has
            # its invitation's own check instead, as before it replies.
            if fence and fence.get('invitation'):
                await fence['invitation'].require_current()
            elif fence:
                # What came from a channel they can't see now (a read, an invited look around, Gene's
                # receipts, a learning report) never reaches the request either. Delivery checks it again.
                # First, so nothing waits between the privacy check below and the request.
                actions = fence['actions']
                if actions and actions.sources and not actions.sources <= set(await actions.channels(fresh=True, around=True)):
                    raise commands.UserFeedbackCheckFailure('my access changed mid-check. ask again.')
                invitation = getattr(actions, 'invitation', None)
                checked = getattr(invitation, 'checked', 0)
                # Every post Fri looked at is still there as it was read: an edit or delete while Fri signed
                # in, or while the access check above waited on Discord, never reaches the model.
                if actions and not await images_unchanged(actions.image_context, actions.event):
                    raise commands.UserFeedbackCheckFailure(IMAGES_CHANGED)
                # A ping's own check before every request, the first too, now that Fri signed in, as an outside
                # ping's (above): its access, the ping, every nearby post its read showed and their authors' privacy.
                # Unless the image check just ran it (fri #75). It refreshes the channel the audience is judged from.
                if invitation and invitation.checked == checked:
                    await invitation.require_current()
                if actions and not await actions.reads_unchanged():  # And every post a read showed.
                    raise commands.UserFeedbackCheckFailure(READ_CHANGED)
                # So is the post they replied to or linked, and the rest of their burst.
                evidence = actions.conversation if actions else {}
                if evidence and (evidence.get('message_burst') or evidence.get('reply_target')):
                    from .fri_conversation import unchanged as conversation_unchanged
                    if not await conversation_unchanged(self.agent, actions.event, evidence):
                        raise commands.UserFeedbackCheckFailure(CONVERSATION_CHANGED)
                # The turn's own gates after those Discord waits, so a member who left or lost the channel
                # during them is caught (see talk).
                if fence.get('gate'):
                    await fence['gate']()
                await self.api('POST', '/chat/turn/check', {**fence['turn'], 'provenance': turn_provenance(
                    context, fence['turn']['user_id'], fence['actions']), **self.summary_generation(context)})
            # A message its author edited or deleted since the live view was copied: its old words stay out.
            # A row read again after that, as an edited message answered again is, holds what it says now.
            # So does one the live view no longer holds as copied: pushed out by newer lines while the turn
            # waited, an edit or delete of it marks nothing, and the copy keeps what it said before (fri #141).
            if self.agent and context.get('recent'):
                where = ((fence or {}).get('turn') or {}).get('conversation_id', '')
                live = self.agent.versions(int(where[7:]) if where.startswith('public:') and where[7:].isdigit() else None)
                context['recent'] = [row for row in context['recent'] if self.agent.current(row, live)]
            # One this turn quotes or replies to refuses it instead: the answer would be about words taken back,
            # which saved turns may still hold. So does one the turn itself answers, its own message or one of
            # its burst, if taken back since it came in (see Agent.rerun).
            if self.agent:
                own = [row.get('source', {}).get('id') for row in context.get('message_burst', []) +
                       ([fence['turn']] if fence and 'turn' in fence else [])]
                since = self.agent.arrival(own)
                if (any(str(row.get('source', {}).get('id')) in self.agent.withdrawn for row in
                        context.get('reply_target', []) + context.get('invited_context', [])) or
                        any(self.agent.taken_back(i, since) for i in own)):
                    raise commands.UserFeedbackCheckFailure(WITHDRAWN)
            # A post it quotes, looked at or read, edited since the turn began (after its last fetch, say): this
            # turn's copy may hold what it said before. The live view's rows show an edit as it comes in. The
            # turn's own messages go by the check above.
            if self.agent and fence and 'began' in fence:
                mine = {int(i) for i in own if str(i).isdigit()}
                def revised(ids):
                    return any(self.agent.revised.get(mid, 0) >= fence['began'] for mid in ids if mid not in mine)
                if revised(quoted):
                    raise commands.UserFeedbackCheckFailure(WITHDRAWN)
                if revised(looked):
                    raise commands.UserFeedbackCheckFailure(IMAGES_CHANGED)
                if revised(read):
                    raise commands.UserFeedbackCheckFailure(READ_CHANGED)
            # A channel closed while Fri gathered context: what was said there stays there.
            context['elsewhere'] = [row for row in context.get('elsewhere', []) if server_wide(
                self.get_channel(int(row['conversation_id'].split(':', 1)[1])))]
            drop_unheard(self.get_channel, context)  # Nor a profile note said there (fri #98).
            # Fri's summary of the member, while it may still show here (their private chat, or a channel everyone
            # can still read, as the gate above read it too) and every channel it came from is still one everyone
            # can read.
            destination = (fence or {}).get('turn') or {}
            here, guild_id = destination.get('conversation_id', ''), destination.get('guild_id', 0)
            if context.get('member_summary'):
                context['member_summary'] = self.member_summary(context['member_summary'], guild_id, here.startswith(
                    'private:') or (self.readable(here, guild_id) is not None and not (fence or {}).get('restricted')))
            if fence:  # Checked again before delivery, with where each profile note shown in public was said.
                fence['shown'].update(row['conversation_id'] for row in context['elsewhere'])
                fence['shown'].update(origin for origin in (context.get('profile_origins') or {}).values()
                                      if str(origin).startswith('public:') and str(origin)[7:].isdigit())
                if context.get('member_summary'):  # Where it came from and, in public, where it shows.
                    origins = context['member_summary'].get('origins', [])
                    fence['shown'].update([*origins, *([here] if here.startswith('public:') else [])])
                    fence.setdefault('summarized', set()).update(origins)  # What the reply drew on (see stage_turn).
                    if GENERATIONS in self.capabilities:  # The one this request carries (see summary_generation).
                        fence.setdefault('summary_generation', context['member_summary'].get('generation'))
            if self.agent:  # Taken back by now: the turn's own message stops it, anyone else's leaves its view.
                source = context.get('source') or {}
                asked = [*([source['id']] if source.get('kind') == 'discord_message' else []),
                         *(row['source']['id'] for row in context.get('message_burst', []))]
                if any(int(mid) in self.agent.unsent for mid in asked):
                    raise GateClosed('message deleted')
                # A post Fri looked at, deleted during the last waits (the gate, Gene's check): its image stays out.
                if any(mid in self.agent.unsent for mid in looked):
                    raise commands.UserFeedbackCheckFailure(IMAGES_CHANGED)
                # So does one it quotes or replies to: the answer would be about words taken back.
                if any(mid in self.agent.unsent for mid in quoted):
                    raise commands.UserFeedbackCheckFailure(WITHDRAWN)
                # And a delete before delivery withholds what the model made of any of it: what it carries, that is,
                # so a live-view row dropped just below (deleted before this request) doesn't count.
                seen.update([*looked, *quoted, *read])
                if 'recent' in context:
                    context['recent'] = [row for row in context['recent']
                                         if row.get('gene_error') or int(row['id']) not in self.agent.unsent]
                    seen.update(int(row['id']) for row in context['recent'] if not row.get('gene_error'))
            # The DM switch again, with nothing to wait on between it and the request: the checks above
            # wait on Discord and Gene, and the owner can turn DMs off meanwhile.
            if fence and fence.get('dms') and not fence['dms']():
                raise GateClosed('dms off')
            # Last, with nothing to wait on before the request: a short line from the member that joined the turn
            # by now is answered with it instead, and one after this is answered on its own (see Continuation).
            if fence and fence.get('commit') and not fence['commit']():
                raise Continued()
        # Every model call of a turn adds to one set, so a delete before delivery withholds it (see reply_once).
        seen = context['fence'].setdefault('messages', set()) if context.get('fence') else set()
        result = await self.chat.respond(text, context, ready=ready)
        result.shown = seen
        attempt = uuid.uuid4().hex
        private_json(self.data / 'usage' / (attempt + '.json'), {
            'attempt_id': attempt, 'model': context['model'], 'effort': context['effort'],
            'status': result.status, 'counters': result.counters})
        try:
            await self.flush_usage()
        except Exception as exc:  # The receipt is on disk; a delivery problem must not discard a finished reply.
            LOG.warning('Usage is saved locally; controller delivery will retry: %s', type(exc).__name__)
        if result.error or result.status != 'completed':
            raise commands.UserFeedbackCheckFailure(result.error or 'that one fizzled on my end. try again in a sec.')
        if agent := getattr(self, 'agent', None):  # What stops it from here was withheld after composing.
            agent.ledger.composed(context)
        return result

    async def talk(self, guild_id, channel_id, user_id, text, visibility, display_name='', *, event=None, burst=(), inspect_images=True, invitation=None, revision=None, typing=None, last_look=None):
        # `revision`: the author's privacy revision as their message came in, if known. Gene fences the
        # reply against the one read below, so an erasure before that read must stop it here.
        # `typing(stage)`: told once a model turn is 'admitted' (no closing, control or cooldown) and
        # before an 'action' runs, so the caller shows typing only when words are coming. `last_look(result)`:
        # the caller's own last Discord read, run before the turn's last privacy checks so they cover it.
        typing = typing or noop
        began = time.time()  # Every copy this turn reads of a post is taken after this (see chat_once).
        if invitation and invitation.outside:
            return await self.invited_talk(invitation, text, typing)
        source = discord_source(event, text, getattr(self.user, 'id', 0)).check(guild_id, f'{visibility}:{channel_id}', user_id)
        heard = [self.hear(item) for item in (event, *(row[1] for row in burst))
                 if getattr(item, 'guild', None) is not None]  # Before anything here waits.
        channel = self.get_channel(channel_id) if self.agent else None
        source_channel = (getattr(channel, 'parent_id', None) or channel_id) if visibility == 'public' and self.agent else self.workshop_id
        scope = {'guild_id': str(guild_id), 'user_id': str(user_id), 'channel_id': str(source_channel),
                 'conversation_id': f'{visibility}:{channel_id}'}
        # Privacy controls must work while inference is busy or the subscription is down.
        intent = memory_intent(text)
        if intent == 'show' and self.profiles:
            context = place_notes(self.get_channel, await self.api('POST', '/chat/context', profiled(self, scope)),
                                  scope['conversation_id'])
            return ChatResult(reply=await self.memory_overview(scope, context['personal_memory'], visibility == 'private',
                                                               context.get('member_summary')),
                              status='completed', memory_scope=scope)
        if intent == 'show':
            context = await self.api('POST', '/chat/context', scope)
            profile = context['personal_memory']
            profile['notes'] = [{**note, 'evidence': discord.utils.escape_markdown(note['evidence'])}
                                for note in profile['notes']]
            return ChatResult(reply=describe_memory(profile), status='completed', memory_scope=scope)
        if intent in ('disable', 'enable'):
            # Deleted or edited after its last check: either may be taking it back, and an erasure can't be undone.
            if self.agent and (getattr(event, 'id', None) in self.agent.unsent or
                               str(getattr(event, 'id', '')) in self.agent.withdrawn):
                raise GateClosed('message deleted')
            await self.api('POST', '/chat/memory/control', {**scope, 'action': intent})
            if self.agent:
                self.agent.forget(user_id, erased=intent == 'disable')
                self.agent.carried_out(user_id, event, intent)
            text = ('done. i erased our saved chats and personal notes, and turned memory off. '
                    'say “remember me again” if you change your mind. discord messages and backups are separate.'
                    if intent == 'disable' else 'memory’s back on. i’ll pick up small preferences as we talk.')
            return ChatResult(reply=text, status='completed')
        from .fri_pending import cancelled
        if source.kind == 'discord_message' and cancelled(text):
            result = await self.api('POST', '/chat/pending/cancel',
                                    {**scope, 'source': source.model_dump(), 'input': text})
            if result['cancelled']:
                return ChatResult(reply='alright, dropped that pending request.', status='completed')
        if user_id in self.busy:
            raise ReplyInProgress()
        if not 1 <= len(text) <= 4000:
            raise commands.UserFeedbackCheckFailure('that’s either empty or a novel. keep it between 1 and 4000 characters.')
        self.busy.add(user_id)
        try:
            async with self.chat_slots:
                context = place_notes(self.get_channel, attributed(await self.api(
                    'POST', '/chat/context', profiled(self, scope))), scope['conversation_id'])
                if revision is not None and context['revision'] != revision:
                    raise AuthorErased()
                if self.profiles:
                    # From Discord's permissions: only a channel everyone could read when each message
                    # arrived, and still can, reaches the member's profile and their other conversations.
                    # Private conversations never do.
                    context['audience'] = ('server' if visibility == 'public' and heard and all(heard) and
                                           server_wide(getattr(event, 'channel', None) or channel) else 'conversation')
                    context['elsewhere'] = self.elsewhere(context.get('elsewhere', []), guild_id)
                    if self.summaries:  # Fri's own summary of the member: their private chats and readable channels only.
                        context['member_summary'] = self.member_summary(
                            context.get('member_summary'), guild_id, visibility == 'private' or context['audience'] == 'server')
                else:
                    context['elsewhere'] = []
                conversation, targets = {}, []
                if self.agent and source.kind == 'discord_message':
                    from .fri_conversation import evidence
                    try:
                        conversation = await evidence(self.agent, event, burst, targets)
                    except ValueError as exc:
                        raise commands.UserFeedbackCheckFailure(str(exc)) from None
                    context.update(conversation)
                    await self.resolve_references(scope, context)
                context['source'] = source.evidence()
                from .fri_addressing import explicit_mention
                context['force_text_reply'] = bool(source.kind == 'discord_message' and
                    (explicit_mention(event, getattr(self.user, 'id', 0)) or
                     any(explicit_mention(row[1], getattr(self.user, 'id', 0)) for row in burst)))
                context['acknowledgments_enabled'] = bool(self.agent and source.kind == 'discord_message' and not context['force_text_reply'])
                from .fri_images import select_source
                image_source = ((invitation.image if invitation else None) or await select_source(event)) if inspect_images else None
                if invitation and image_source:
                    invitation.image = image_source
                if image_source is None and quick_closure(
                        text, {**context, 'recent': self.agent.messages(channel_id) if self.agent else []}):
                    result = ChatResult(status='completed', response_kind='react', reaction='👍')
                    result = self.stage_turn(scope, source, text, context, result, model='local-closing', derived=False)
                    await self.api('POST', '/chat/turn/check', result.turn)  # Fence even a reaction.
                    return result
                if user_id == self.owner_id and guild_id == self.guild_id and usage_requested(text):
                    window, label, windowless = usage_query(text, self.capabilities, time.time())
                    usage = await self.api('POST', '/agent/usage',
                                           {'guild_id': str(guild_id), 'user_id': str(user_id), **window})
                    if label and not window_applied(usage, window):
                        label, windowless = None, True  # The controller didn't apply it, so these are all-time.
                    report = describe_usage(usage, by_model='by model' in ' '.join(text.casefold().split()),
                                            window=label, windowless=windowless)
                    result = self.stage_turn(scope, source, text, context, ChatResult(reply=report, status='completed'),
                                             model='local-usage-report', derived=False)
                    await self.api('POST', '/chat/turn/check', result.turn)  # An erasure mid-turn stops it.
                    return result
                if self.agent and self.agent.gave_way(event):
                    raise Continued()  # A short line joined it first: nothing here is reserved, run or answered.
                from .fri_operational import admit as admit_operation
                operation = await admit_operation(self, event, source, scope, text, context)
                if operation.result:
                    if operation.result.status == 'completed':
                        saved_result = (ChatResult(reply='Earlier check: gene_command. Fetch current evidence for follow-up details; no findings are retained here.', status='completed')
                                        if operation.tool_receipt else operation.result)
                        operation.result.turn = self.stage_turn(scope, source, text, context, saved_result,
                                                                model='local-control', derived=False).turn
                        operation.result.turn['verbatim'] = saved_result is operation.result
                        try:
                            await self.api('POST', '/chat/turn/check', operation.result.turn)
                        except commands.UserFeedbackCheckFailure:
                            if not operation.tool_receipt:
                                raise
                            from .fri_outcomes import unrecorded
                            return ChatResult(reply=unrecorded([operation.tool_receipt]), status='completed')
                    return operation.result
                if self.agent and self.agent.config['enabled'] and not operation.continuation:
                    # Existing local/operational work keeps its own admission and
                    # allowance. A mention charges social budget only for new chat.
                    # A burst a short line reopened was already admitted, as the turn it joined: one exchange.
                    admission = (await invitation.admit() if invitation else self.agent.readmitted(event) or
                        await self.api('POST', '/agent/admit', {**self.agent.identity(user_id, guild_id),
                            'channel_id': str(source_channel),
                            'space': 'dm' if visibility == 'private' else 'public'}))
                    if admission['allowed']:
                        self.agent.admitted(event)
                    if not admission['allowed']:
                        minutes = max(1, int((admission['until']-time.time()+59)//60))
                        notice = (f'bro im cooked rn, gotta recharge my social battery and save daniel’s ai wallet lol. '
                                  f'back in about {minutes} mins') if admission.get('notice') else ''
                        return ChatResult(reply=notice, status='cooldown')
                async def sources_unchanged():  # The reply target and burst, as Discord has them now.
                    if conversation and (conversation.get('message_burst') or conversation.get('reply_target')):
                        from .fri_conversation import unchanged
                        if not await unchanged(self.agent, event, conversation):
                            raise commands.UserFeedbackCheckFailure('the conversation changed while i was typing. ask again and i’ll catch up.')

                async def looked(result):  # The caller's last look, then the sources again: it awaited.
                    if last_look:
                        await last_look(result)
                        await sources_unchanged()

                async def as_read():
                    """Everything read from Discord before typing started, as it stands now: typing waited on
                    Discord, and nothing edited or deleted meanwhile may reach the model or a tool."""
                    if invitation:
                        await invitation.require_current()  # The ping, its access and what it collected.
                    elif self.agent and source.kind == 'discord_message':
                        try:
                            current = await event.channel.fetch_message(event.id)
                        except discord.HTTPException:
                            raise GateClosed('message deleted') from None  # Quiet, as for any taken-back message.
                        if current.edited_at != self.agent.edits.get(event.id) or current.content != event.content:
                            self.agent.saw_edit(event.id, current.edited_at)  # A re-run waits for Discord to show it.
                            self.agent.withdraw(event)  # Marked, so its edit is what gets answered (see Agent.reply_once).
                            raise commands.UserFeedbackCheckFailure('the conversation changed while i was typing. ask again and i’ll catch up.')
                    await sources_unchanged()
                    from .fri_images import CHANGED as IMAGES_CHANGED
                    from .fri_images import still_there
                    if image_source is not None and image_source is not event and not await still_there(image_source):
                        raise commands.UserFeedbackCheckFailure(IMAGES_CHANGED)

                if await typing('admitted'):
                    await as_read()
                if invitation:
                    if invitation.grant and event.reference and event.reference.message_id:
                        await invitation.collect(include_recent=False)
                    context.update(invitation.context())
                    image_source = invitation.image or image_source
                if self.agent:
                    context.update(await self.agent.extra_context(channel_id, user_id, visibility == 'public'))
                actions = Actions(self.agent, user_id, channel_id, visibility == 'public') if self.agent else None
                if actions:
                    actions.invitation = invitation
                    actions.turn_context = context
                    actions.event, actions.scope, actions.revision = event, scope, context['revision']
                    actions.message, actions.model, actions.effort = clean(text, 4000), context['model'], context['effort']
                    actions.web_research = context.get('web_research', [])
                    actions.pending = context.get('pending')
                    actions.continuation_only = actions.pending['goal'] if operation.continuation else None
                    actions.conversation = conversation
                    actions.reopened = getattr(burst, 'origin', None) is not None  # See Continuation.
                    actions.private_evidence = bool(conversation.get('reply_target') or context.get('invited_context'))
                    actions.media_allowed = inspect_images
                context['actions'] = await actions.available() if actions else {}
                if operation.continuation:
                    goal = actions.pending['goal']
                    permitted = 'web_search' if goal == 'web' else 'gene_command'
                    context['actions'] = {k: v for k, v in context['actions'].items() if k == permitted}
                    if goal != 'web' and permitted in context['actions']:
                        control = context['actions'][permitted]
                        control['commands'] = {k: v for k, v in control['commands'].items() if k == goal}
                context['display_name'] = source.display_name
                context['speaker'] = {'user_id': source.user_id, 'display_name': source.display_name}
                context['requests_enabled'] = guild_id == self.guild_id
                context['requester_is_owner'] = user_id == self.owner_id
                from .fri_images import attach as attach_images
                if inspect_images:
                    await attach_images(context, event, text, source=image_source, invitation=invitation)
                    from .fri_images import look, lookable, own_links
                    await own_links(context, event, invitation=invitation)  # Their own link's card, unfurled after it came in.
                    if targets and not context.get('unattributed_reference'):
                        # The meme they replied to or linked: a still image, a GIF link's preview or a link card's picture.
                        before = len(context.get('image_reads', []))
                        await look(context, [post for post in targets if lookable(post)])
                        for read in context.get('image_reads', [])[before:] if invitation else ():
                            # Checked on their own refetch before each request; a ping's delivery check is its
                            # invitation's, so that finds them again too (see Actions.valid).
                            invitation.inspected(read['event'], read['sources'])
                if actions:
                    from .fri_images import image_context
                    actions.image_context = image_context(context)
                if actions and guild_id == self.guild_id:
                    from .gene_controls import current_music_context, grounded
                    recent = await self.api('POST', '/controls/recent', scope)
                    readable = await actions.channels(fresh=True)
                    context['gene_receipts'] = grounded([r for r in recent.get('receipts', []) if r['channel_id'] in readable])
                    actions.sources.update(r['channel_id'] for r in context['gene_receipts'])
                    actions.music_context = current_music_context(context['gene_receipts'])
                    actions.music_receipts = context['gene_receipts']
                    actions.music_correctable = recent.get('correctable')  # None from a controller before it.
                text = clean(text, 4000)
                context['fence'] = {'turn': {**scope, 'revision': context['revision'], 'source': source.model_dump(),
                                             'input': text, 'reply': '', 'model': context['model'],
                                             'effort': context['effort']},
                                    'actions': actions, 'shown': set(), 'began': began}
                if self.agent and source.kind == 'discord_message':
                    context['fence']['commit'] = lambda: self.agent.commit(event)
                if self.agent and visibility == 'private' and source.kind == 'discord_message':
                    async def gate():  # A DM's own gates: its author still a member, as Discord has it now, and DMs on.
                        if closed := await self.agent.dm_closed(user_id):
                            raise GateClosed(closed)
                    context['fence']['gate'] = gate  # Run with the other last checks (see chat_once).
                    context['fence']['dms'] = lambda: self.agent.config['support_dms']
                elif self.agent and source.kind == 'discord_message':
                    async def gate():
                        """A channel message's own gates: its author can still read the channel (fri #31), and
                        whether everyone can (fri #97), both from fetched overwrites and roles, not the cache.
                        A ping's own check read the channel already (see Invitation.access)."""
                        if invitation:
                            fetched = invitation.fetched
                        else:
                            member = await self.agent.member(user_id, fresh=True, guild_id=guild_id)
                            if not member or not (fetched := await self.agent.can_read(event, member)):
                                raise GateClosed('member changed')
                        if not server_wide(event.channel, fetched):
                            context['fence']['restricted'] = True  # For the rest of the turn (see audience).
                    context['fence']['gate'] = gate
                result = await self.chat_once(text, context)
                # A ping's last check before sending (Agent.reply_once) covers it and its image, and a tool
                # checks it again before acting, so nothing here repeats that.
                if conversation and (conversation.get('message_burst') or conversation.get('reply_target')):
                    from .fri_conversation import unchanged
                    if not await unchanged(self.agent, event, conversation):
                        raise commands.UserFeedbackCheckFailure(CONVERSATION_CHANGED)
                from .fri_images import require_current as require_current_images
                if not invitation:
                    await require_current_images(context, event)
                if guild_id != self.guild_id:
                    result.draft = None
                if actions and actions.private_evidence:
                    result.draft, result.notes = None, []
                if result.action:
                    if not actions or result.action['kind'] not in context['actions']:
                        raise commands.UserFeedbackCheckFailure('nah, that’s not available here.')
                    if await typing('action'):  # Checked before typing started: again, before the tool sees anything.
                        await as_read()
                        await self.api('POST', '/chat/turn/check', {**context['fence']['turn'], 'provenance': turn_provenance(
                            context, user_id, actions), **self.summary_generation(context)})
                    from .fri_runner import run
                    result, receipts, research = await run(self, actions, text, context, result)
                    result.shown = context['fence'].get('messages', set())  # A summary of the steps may still draw on them.
                    if actions.private_evidence:
                        result.draft, result.notes = None, []
                    if context.get('force_text_reply'):
                        from .fri_mentions import force_text
                        result = force_text(result)
                    if not invitation:  # A ping's is its last check before sending.
                        await require_current_images(context, event)
                    receipt = receipts[-1]
                    action_kind = receipt['kind']
                    from .fri_outcomes import unrecorded
                    try:
                        current = await self.api('POST', '/chat/context', scope)
                    except (commands.UserFeedbackCheckFailure, TimeoutError):
                        return ChatResult(reply=unrecorded(receipts, reason='I couldn’t recheck conversation access to show the details.'), status='completed')
                    if current['revision'] != context['revision'] or not await actions.valid(sending=True):
                        return ChatResult(reply=unrecorded(receipts, reason='Access or memory changed before I could safely show the details.'), status='completed')
                    result.notes = []
                    diagnosis = (receipt if action_kind == 'gene_diagnose'
                                 else receipt.get('gene_result', receipt).get('diagnostic', {}))
                    # An action's proposal used to be discarded here. Keep the checked diagnosis
                    # as a real preview unless the controller accepted it for automatic verification.
                    proposal = diagnosis.get('proposed_request', '')
                    result.draft = proposal if len(proposal) >= 5 and not actions.private_evidence else None
                    if research or receipt.get('sources'):
                        from .fri_web import cite_reply, links_requested
                        link_request = links_requested(text) or links_requested((actions.pending or {}).get('request_text', ''))
                        result.reply = cite_reply(result.reply, research or receipt,
                            include_sources=action_kind == 'web_sources' or (link_request and action_kind != 'image_search'))
                    if action_kind == 'image_search' and receipt.get('image_status') == 'prepared':
                        result.media = actions.image_share.prepared
                    stored, private = result.reply, private_read(receipts)
                    crossed, recalled = linked_elsewhere(context, source), summarized_elsewhere(context)
                    if private and not (result.clarification or result.settings_preview or result.post_preview):
                        # Other channels' messages, archives and reports never become saved chat text.
                        checked_channel = receipt.get('channel_id', '')
                        stored = f'Earlier check: {action_kind}' + (f' in channel {checked_channel}' if checked_channel else '')
                        stored += '. Fetch current evidence for follow-up details; no findings are retained here.'
                    elif crossed and not (result.clarification or result.settings_preview or result.post_preview):
                        stored = LINKED
                    elif recalled and not (result.clarification and actions.private_evidence or
                                           result.settings_preview or result.post_preview):
                        # Only the harness's fixed question (private evidence) derives nothing; a model-written
                        # clarification can carry the summary, so its saved and pending copies go neutral too.
                        stored = SUMMARIZED
                        if result.clarification:
                            result.clarification = {**result.clarification, 'question': SUMMARIZED_QUESTION}
                    try:  # A fixed harness question over referenced evidence derives nothing from it.
                        provenance = ([] if result.clarification and actions.private_evidence
                                      else turn_provenance(context, user_id, actions))
                    except commands.UserFeedbackCheckFailure:
                        return ChatResult(reply=unrecorded(receipts), status='completed')
                    result.turn = {**scope, 'revision': context['revision'], 'source': source.model_dump(),
                        'input': clean(event.content, 4000) if result.clarification or context.get('message_burst') else text,
                        'reply': stored, 'model': context['model'], 'effort': context['effort'], 'notes': [],
                        'web_receipt': research, 'clarification': result.clarification,
                        'burst': context.get('message_burst', []),
                        'allow_closing': result.allow_closing and not bool(result.draft or result.settings_preview or
                                                                         result.post_preview or result.clarification),
                        'provenance': provenance, 'verbatim': stored == result.reply, **self.summary_generation(context),
                        **({'audience': audience(context)} if context.get('audience') else {})}
                    if len(result.turn['provenance']) > MAX_PROVENANCE:
                        return ChatResult(reply=unrecorded(receipts, reason='too many people were in that context to '
                                                           'track safely.'), status='completed')
                    if context.get('unattributed_reference'):
                        result.turn.update(reply=UNATTRIBUTED, verbatim=False)
                    elif not private and not crossed and not recalled:
                        result.echo = self.echo(scope, context, result.turn['provenance'], stored)
                    await looked(result)
                    try:  # The save's privacy fence, applied before anything is shown.
                        await self.api('POST', '/chat/turn/check', result.turn)
                        self.still_open(context)
                    except commands.UserFeedbackCheckFailure:
                        return ChatResult(reply=unrecorded(receipts), status='completed')
                    return result
                if context.get('gene_receipts') and not await actions.valid(outage=True, sending=True):
                    raise commands.UserFeedbackCheckFailure('my access changed mid-check. ask again.')
                result = self.stage_turn(scope, source, text, context, result, actions=actions)
                await looked(result)
                # The save's privacy fence runs before delivery; the save itself waits for delivery.
                await self.api('POST', '/chat/turn/check', result.turn)
                self.still_open(context)
                return result
        finally:
            self.busy.discard(user_id)

    def stage_turn(self, scope, source, text, context, result, *, model=None, actions=None, derived=True):
        """Prepare the turn record; record_turn saves it only after Discord accepts the delivery."""
        if context.get('force_text_reply'):
            from .fri_mentions import force_text
            result = force_text(result)
        if context.get('message_burst'):
            text = context['message_burst'][-1]['text']
        # Referenced members' words may shape the reply. It is kept with provenance so their
        # erasure reaches it, but never becomes this member's personal notes or a request draft.
        if context.get('reply_target') or context.get('invited_context'):
            result.draft, result.notes = None, []
        provenance = turn_provenance(context, scope['user_id'], actions) if derived else []
        if len(provenance) > MAX_PROVENANCE:  # Never drop dependencies; refuse instead.
            raise commands.UserFeedbackCheckFailure('too many people were in that context for me to keep track '
                                                    'of safely. ask again in a bit.')
        reply, unattributed = result.reply, derived and context.get('unattributed_reference')
        crossed = derived and linked_elsewhere(context, source)
        recalled = derived and summarized_elsewhere(context)
        if unattributed:  # A referenced Fri message whose sources are unknown is never retained.
            reply, result.notes = UNATTRIBUTED, []
        elif crossed:
            reply = LINKED
        elif recalled:
            reply = SUMMARIZED
        result.turn = {**scope, 'revision': context['revision'], 'source': source.model_dump(), 'input': text,
            'reply': reply, 'response_kind': result.response_kind, 'reaction': result.reaction,
            'allow_closing': result.allow_closing, 'burst': context.get('message_burst', []),
            'model': model or context['model'], 'effort': '' if model else context['effort'],
            'notes': result.notes, 'input_tokens': 0 if model else result.counters.get('input_tokens'),
            'output_tokens': 0 if model else result.counters.get('output_tokens'), 'provenance': provenance,
            'verbatim': result.response_kind == 'reply' and reply == result.reply,
            **(self.summary_generation(context) if derived else {}),
            **({'audience': audience(context)} if context.get('audience') else {})}
        if derived and result.response_kind == 'reply' and not unattributed and not crossed and not recalled:
            result.echo = self.echo(scope, context, provenance, reply)
        return result

    def still_open(self, context):
        """Before delivery: every other channel whose words reached the model is still one everyone can read."""
        for conversation in (context.get('fence') or {}).get('shown', ()):
            if not server_wide(self.get_channel(int(conversation.split(':', 1)[1]))):
                raise commands.UserFeedbackCheckFailure(CLOSED)

    def readable(self, conversation, guild_id):
        """The channel of a public conversation in this server, if everyone in it can still read it."""
        kind, _, channel_id = str(conversation).partition(':')
        channel = self.get_channel(int(channel_id)) if kind == 'public' and channel_id.isdigit() else None
        if (channel is None or getattr(getattr(channel, 'guild', None), 'id', None) != int(guild_id)
                or not server_wide(channel)):
            return None  # Gone, moved, or restricted since: what was said there stays there.
        return channel

    def elsewhere(self, rows, guild_id):
        """What the member said lately in other channels that everyone in this server can still read."""
        kept = []
        for row in rows:
            channel = self.readable(row.get('conversation_id', ''), guild_id)
            if channel is not None:
                kept.append({**row, 'channel': '#'+channel.name, 'minutes_ago': max(0, int((time.time()-row['at'])//60))})
        return kept

    def member_summary(self, summary, guild_id, shown):
        """Fri's own summary of the member where it may show, while every channel it came from is still readable.
        From a controller offering summary generations, only with one a turn can carry (see summary_generation)."""
        if not summary or not shown or not all(self.readable(origin, guild_id) for origin in summary.get('origins', [])):
            return None
        if GENERATIONS in self.capabilities and not (type(summary.get('generation')) is int and summary['generation'] >= 0):
            return None
        return summary

    def summary_generation(self, context):
        """What a turn's checks and save carry for Fri's summary of the member its model request had (gene #91): that
        summary's generation, from the same /chat/context response. A withdrawal dropping the summary since moves it,
        so the controller refuses the turn and its reply is withheld. The first one a request carried (see chat_once),
        else the summary the next request is about to carry, for the check right before it. Nothing when no summary
        reached the model, or from a controller without summary generations."""
        if GENERATIONS not in self.capabilities:
            return {}
        fence = context.get('fence') or {}
        generation = fence.get('summary_generation', (context.get('member_summary') or {}).get('generation'))
        return {'summary_generation': generation} if type(generation) is int else {}

    async def memory_overview(self, scope, profile, private, summary=None):
        """Everything for the member's own eyes in private; in public, only what that channel can see."""
        escape = discord.utils.escape_markdown
        if private:
            state = await self.api('POST', '/chat/memory/notes', profiled(self, scope))
            enabled, rows = state['enabled'], state['entries']
            summary = escape(state['summary']['text']) if state.get('summary') else None
        else:
            enabled, rows = profile['enabled'], profile['notes']
            # Its words stay out of a public channel; that it exists may show where it would itself.
            summary = bool(self.member_summary(summary, scope['guild_id'], self.readable(
                scope['conversation_id'], scope['guild_id']) is not None))
        groups = {}
        for row in rows:
            where = note_place(self.get_channel, row['scope'], scope['conversation_id'], public=not private)
            groups.setdefault(where, []).append(escape(row['evidence']))
        return describe_profile(enabled, list(groups.items()), private, summary)

    async def resolve_references(self, scope, context):
        """Replace Fri-authored reply targets with the members those replies drew on."""
        own = [row['source']['id'] for row in context.get('reply_target', [])
               if row['source'].get('user_id') == str(getattr(self.user, 'id', ''))]
        if not own:
            return
        try:
            found = (await self.api('POST', '/chat/messages/sources',
                                    {'guild_id': scope['guild_id'], 'message_ids': own}))['messages']
        except commands.UserFeedbackCheckFailure:
            found = {}
        unresolved = {mid for mid in own if mid not in found or not found[mid]['verbatim']}
        if unresolved.difference(found):
            # Patch notes Fri posted are its own words, drawing on no member. Only an id Gene doesn't know,
            # in the channel the record names: a reply Gene dropped (an erasure, say) is never stood in for.
            notes, where = self.posted_notes(), {row['source']['id']: row['source'].get('channel_id')
                                                 for row in context['reply_target']}
            unresolved = {mid for mid in unresolved if mid in found or mid not in notes or notes[mid] != where[mid]}
        if unresolved:
            # Fail closed before inference: the model never sees text whose sources erased or were never
            # attributable, and nothing derived is retained. A copy keeps the integrity checks intact.
            context['unattributed_reference'] = True
            context['reply_target'] = [{**row, 'text': UNAVAILABLE} if row['source']['id'] in unresolved else row
                                       for row in context['reply_target']]
        context['reference_provenance'] = [item for mid in own for item in found.get(mid, {}).get('provenance', [])]

    def posted_notes(self):
        """Message id -> channel id of every patch-notes part Discord confirmed, in #fri-chat or the owner's DM.
        A damaged record names none, so those posts stay unavailable."""
        from .fri_patch_notes import NotesRefused, load
        posted = {}
        for name in ('patch-notes-server.json', 'patch-notes.json'):
            try:
                state = load(self.data/name)
            except NotesRefused:
                continue
            posted.update((part['message_id'], part['channel_id']) for entry in state['versions'].values()
                          for part in entry['parts'] if 'message_id' in part)
        return posted

    @staticmethod
    def echo(scope, context, provenance, reply):
        """Fri's delivered public reply for the shared channel view, tagged with whose words shaped it."""
        if not scope['conversation_id'].startswith('public:') or not context.get('personal_memory', {}).get('enabled', True):
            return None
        return {'text': reply, 'provenance': provenance + [{'user_id': str(scope['user_id']),
                                                            'revision': context['revision']}]}

    async def record_turn(self, result, sent=None):
        """Save a staged turn once delivered. Delivery is the evidence Fri actually said it."""
        payload, result.turn = result.turn, None
        if not payload:
            return None
        if isinstance(getattr(sent, 'id', None), int) and payload.get('response_kind', 'reply') == 'reply':
            payload = {**payload, 'reply_message_id': str(sent.id)}  # Later references resolve to its sources.
            delivered = getattr(sent, 'content', None)
            if payload.get('verbatim') and isinstance(delivered, str) and delivered != payload['reply']:
                payload['reply'] = clean(delivered, 3500)  # E.g. an attachment fallback replaced the answer.
        if payload.get('audience') == 'server' and not server_wide(
                self.get_channel(int(payload['conversation_id'].split(':', 1)[1]))):
            payload = {**payload, 'audience': 'conversation'}  # Closed while Fri replied: it stays where it was said.
        began = time.time(), time.monotonic()
        try:
            saved = await self.api('POST', '/chat/turn', payload)
        except (commands.UserFeedbackCheckFailure, ControllerRejected, TimeoutError) as exc:
            LOG.warning('Delivered turn was not recorded: %s', type(exc).__name__)  # Includes privacy changes mid-reply.
            if isinstance(exc, (TimeoutError, ControllerUnavailable)) and self.agent:  # A lost reply, not a refusal:
                self.agent.saved_turns.kept(payload, None, began)  # it may have landed all the same.
            return None
        if self.agent:  # So its messages taken back later take it with them (fri #129).
            self.agent.saved_turns.kept(payload, saved, began)
        if saved.get('first_memory') and sent is not None and payload.get('response_kind', 'reply') == 'reply':
            notice = self.first_memory(saved)
            content = (sent.content or '')+notice
            try:
                if len(content.encode('utf-16-le'))//2 <= 2000:
                    await sent.edit(content=content)
                else:  # No room left in a long reply: say it on its own rather than lose it.
                    await sent.reply(notice.strip(), allowed_mentions=NO_MENTIONS)
            except discord.HTTPException:
                LOG.info('First-memory notice was not added')
        return saved

    @staticmethod
    def mention_cooldown(admission):
        minutes = max(1, int((admission.get('until', time.time())-time.time()+59)//60))
        return ChatResult(reply=(f'bro im cooked rn, gotta recharge my social battery and save daniel’s ai wallet lol. '
                                f'back in about {minutes} mins') if admission.get('notice') else '', status='cooldown')

    async def invited_talk(self, invitation, text, typing=None):
        """Outside the ambient scope, one ping has only conversation and public research."""
        from .fri_context import addressed_content
        from .fri_images import attach, image_context, own_links
        from .fri_mentions import force_text
        event, user = invitation.event, invitation.event.author.id
        expected = addressed_content(event.content, self.user.id) or 'hey'
        if text != expected or not 1 <= len(text) <= 4000:
            raise commands.UserFeedbackCheckFailure('that’s either empty or a novel. keep it between 1 and 4000 characters.')
        # Match the raw live event first; only minimized text may reach a model or tool.
        text = clean(text, 4000)
        if user in self.busy:
            raise ReplyInProgress()
        self.busy.add(user)
        try:
            async with self.chat_slots:
                admission = await invitation.admit()
                if not admission['allowed']:
                    return self.mention_cooldown(admission)
                await (typing or noop)('admitted')
                scope = invitation.granted_scope()
                context = attributed(await self.api('POST', '/chat/context', scope))
                await invitation.collect()
                context.update(invitation.context())
                context.update(source=discord_source(event).evidence(), requests_enabled=False,
                               requester_is_owner=user == self.owner_id,
                               speaker={'user_id': str(user), 'display_name': clean(event.author.display_name, 80)})
                actions = Actions(self.agent, user, event.channel.id, True)
                actions.invitation, actions.event, actions.scope = invitation, event, scope
                actions.revision = context['revision']
                actions.web_research = context.get('web_research', [])
                actions.message, actions.model, actions.effort = clean(text, 4000), context['model'], context['effort']
                actions.private_evidence = True
                context['actions'] = await actions.available()
                await attach(context, event, text, source=invitation.image, invitation=invitation)
                await own_links(context, event, invitation=invitation)  # The ping's own card, before the nearby posts' pictures.
                await invitation.look(context)  # Up front: an outside ping has no read to decide on.
                actions.image_context = image_context(context)
                # See chat_once. What a Fri answer it reads drew on counts as shown: a delete of it withholds the reply.
                context['fence'] = {'invitation': invitation, 'shown': set(), 'messages': set(invitation.carried)}
                result = await self.chat_once(text, context)
                research = None
                if result.action:
                    if result.action['kind'] not in context['actions']:
                        raise commands.UserFeedbackCheckFailure('nah, that’s not available here.')
                    await (typing or noop)('action')
                    from .fri_runner import run
                    result, receipts, research = await run(self, actions, text, context, result)
                    receipt = research or receipts[-1]
                    if receipt.get('sources'):
                        from .fri_web import cite_reply, links_requested
                        result.reply = cite_reply(result.reply, receipt,
                            include_sources=receipts[-1]['kind'] == 'web_sources' or links_requested(text))
                result = force_text(result)
                result.draft, result.notes = None, []
                # Its last check before sending (Agent.reply_once) covers it and its image.
                # The controller stores only a neutral placeholder for outside invitations.
                result.turn = {**scope, 'revision': context['revision'],
                    'input': text, 'reply': result.reply, 'source': invitation.source,
                    'model': context['model'], 'effort': context['effort'], 'notes': [],
                    'burst': [], 'clarification': None,
                    'response_kind': 'reply', 'reaction': None, 'allow_closing': False,
                    'input_tokens': result.counters.get('input_tokens'), 'output_tokens': result.counters.get('output_tokens'),
                    'web_receipt': research}
                return result
        finally:
            self.busy.discard(user)

    async def chat_interaction(self, interaction, text):
        if memory_intent(text) == 'show':
            from .fri_memory_panel import show
            await show(self, interaction)
            return
        await defer_panel(interaction)
        await reply(self, interaction, 'Thinking…', view=HomeView(self, interaction.user.id))
        try:
            result = await self.talk(interaction.guild_id, interaction.channel_id, interaction.user.id, text, 'private',
                                     interaction.user.display_name, event=interaction)
            view = (DraftView(self, interaction.user.id, result.draft, fence=result.turn) if result.draft
                    else HomeView(self, interaction.user.id))
            if result.settings_preview:
                from .fri_agent import BehaviorPreview
                view = BehaviorPreview(self.agent, **result.settings_preview)
                await reply(self, interaction, view.content, attachments=[], view=view, verbatim=True)
            else:
                await reply(self, interaction, embed=card('Fri', result.reply), view=view)
            # A newer panel interaction can supersede this one; record only what was displayed.
            saved = await self.record_turn(result) if sessions(self).displayed(interaction) else None
            if saved and saved.get('first_memory') and not result.settings_preview:
                await reply(self, interaction, embed=card('Fri', result.reply+self.first_memory(saved)), view=view)
        except commands.UserFeedbackCheckFailure as exc:
            await reply(self, interaction, str(exc), view=HomeView(self, interaction.user.id))
        await self.waiting_notices.clear(interaction.user.id)

    async def on_ready(self):
        if self.agent and self.profiles:
            channels = [self.get_channel(int(c)) for c in self.agent.state.get('channels', []) if str(c).isdigit()]
            LOG.info('Member profiles: %d of %d configured channels are readable by everyone',
                     sum(server_wide(c) for c in channels), len(channels))

    # With no message cache, Discord's deletes and edits arrive only as raw events.
    async def on_raw_message_delete(self, payload):
        if self.agent:
            self.agent.unsend(payload.channel_id, [payload.message_id])

    async def on_raw_bulk_message_delete(self, payload):
        if self.agent:
            self.agent.unsend(payload.channel_id, payload.message_ids)

    async def on_raw_message_edit(self, payload):
        if self.agent:
            self.agent.edit(payload.channel_id, payload.message)

    async def on_message(self, message):
        if self.agent:
            try:
                await self.agent.handle(message)
            except (commands.UserFeedbackCheckFailure, discord.HTTPException) as exc:
                LOG.warning('Fri interaction could not complete: %s', type(exc).__name__)
            return
        if (message.author.bot or getattr(message, 'webhook_id', None) or not message.guild or
                message.guild.id != self.guild_id):
            return
        if (getattr(message.channel, 'parent_id', None) or message.channel.id) != self.workshop_id:
            return
        text = await addressed_text(message, self.user.id)
        if text is None:
            return
        try:
            async with message.channel.typing():
                result = await self.talk(message.guild.id, message.channel.id, message.author.id, text, 'public',
                                         message.author.display_name, event=message)
            view = DraftView(self, message.author.id, result.draft, public=True, fence=result.turn) if result.draft else None
            sent = await message.reply(result.reply, view=view, mention_author=False, allowed_mentions=NO_MENTIONS,
                                       suppress_embeds=True)
            await self.record_turn(result, sent)
            await self.waiting_notices.clear(message.author.id)
        except ReplyInProgress:
            await self.waiting_notices.send(message)
        except commands.UserFeedbackCheckFailure as exc:
            await message.reply(notice_text(str(exc))[:1800], mention_author=False, allowed_mentions=NO_MENTIONS)
            await self.waiting_notices.clear(message.author.id)

    async def subscription_notice(self):
        """One owner DM per refused sign-in, counting a usable token in between as the end of one."""
        try:
            refused = self.chat.subscription.refused
            if refused is not None and refused != self.noticed_refusal:
                owner = self.get_user(self.owner_id) or await self.fetch_user(self.owner_id)
                channel = await owner.create_dm()  # Opened here, so a failure to open it retries next tick.
                self.noticed_refusal = refused  # At most once: a send whose response was lost isn't repeated.
                await channel.send(SIGN_IN_NOTICE, allowed_mentions=NO_MENTIONS)
        except Exception as exc:  # A failed lookup retries next tick; a failed send waits for the next refusal.
            LOG.warning('Owner sign-in notice not sent: %s', type(exc).__name__)

    @tasks.loop(seconds=15)
    async def owner_reviews(self):
        # Its own task: a rate-limited DM sleeps out Discord's retry_after, which must not hold up reviews or usage.
        if self.sign_in_notice is None or self.sign_in_notice.done():
            self.sign_in_notice = asyncio.create_task(self.subscription_notice())
        # Separate, so one undeliverable usage receipt can't hold back owner notifications.
        try:
            await self.flush_usage()
        except Exception as exc:  # Like the heartbeat, a bad tick never ends the loop.
            LOG.warning('Usage delivery pending: %s', type(exc).__name__)
        try:
            await notify_owner(self)
        except Exception as exc:
            LOG.warning('Owner notification pending: %s', type(exc).__name__)

    @owner_reviews.before_loop
    async def before_reviews(self):
        await self.wait_until_ready()

    async def keep_watch(self):
        # Its own task: a stuck heartbeat or owner DM can't hide an outage. Not a tasks.loop, which schedules
        # by the wall clock: a clock set back an hour mid-pass would stall it an hour. asyncio sleeps on the
        # event loop's monotonic clock.
        from .fri_watch import INTERVAL
        await self.before_watch()
        clock = asyncio.get_running_loop()
        while True:
            started = clock.time()
            try:
                await self.watch()
            except Exception as exc:  # A watchdog that stops on one odd error watches nothing.
                LOG.warning('Gene watch pass failed: %s', type(exc).__name__)
            await asyncio.sleep(max(0, started+INTERVAL-clock.time()))

    async def watch(self):
        # Reads stay an interval apart across startup's wait, the first pass and restarts; a pending DM
        # still goes out on every pass.
        await self.watcher.check(observe=self.watcher.due(slack=15))

    async def before_watch(self):
        # No wait for the gateway: the watch reads and DMs over HTTP, as it does during startup's wait.
        await asyncio.sleep(self.watcher.wait())  # Not a pass early, which would skip its read and wait a whole one more.

    @tasks.loop(seconds=10)
    async def health(self):
        guild = self.get_guild(self.guild_id)
        test = self.agent.state.get('test_scope', {}) if self.agent else {}
        test_guild = self.get_guild(int(test['guild_id'])) if test.get('guild_id', '').isdigit() else None
        test_channel = self.get_channel(int(test['channel_id'])) if test.get('channel_id', '').isdigit() else None
        test_permissions = test_channel.permissions_for(test_guild.me) if (
            test_channel and test_guild and test_guild.me and test_channel.guild.id == test_guild.id) else None
        private_json(self.data / 'fri-health.json', {'at': time.time(),
            'rotating_status': self.agent.presence.receipt if self.agent and self.agent.presence else None,
            'meme_attention': self.agent.memes.status() if self.agent and self.agent.memes else None,
            'agent_heartbeat': {'running': self.agent.heartbeat.is_running(), 'failed': self.agent.heartbeat.failed(),
                'started': self.agent.heartbeat_started, 'finished': self.agent.heartbeat_finished,
                'status': self.agent.heartbeat_status, 'stages': self.agent.stages,
                'current': self.agent.heartbeat_current} if self.agent else None,
            'outcomes': self.agent.ledger.snapshot() if self.agent else None,
            'rejected_usage': len(list((self.data / 'usage' / 'rejected').glob('*.json'))),
            'guild_connected': bool(self.is_ready() and guild and not guild.unavailable),
            'sha': os.getenv('FRI_RELEASE_SHA', 'development'), 'guild_id': self.guild_id,
            'test_scope': {**test, 'connected': bool(self.is_ready() and test_guild and not test_guild.unavailable),
                           'read_send_history': bool(test_permissions and test_permissions.view_channel and
                                test_permissions.send_messages and test_permissions.read_message_history)},
            'presence': str(self._connection._status)})

    @health.before_loop
    async def before_health(self):
        await self.wait_until_ready()

    async def close(self):
        if self.agent:
            self.agent.close()
        self.health.cancel()
        self.owner_reviews.cancel()
        if self.watching:
            self.watching.cancel()
        if self.sign_in_notice:
            self.sign_in_notice.cancel()
        await sessions(self).close_all()
        if self.session:
            await self.session.close()
        await super().close()


def main():
    from .logging import configure, install_secret_filter
    install_secret_filter()
    configure()
    from .fri_instance import instance_lock
    with instance_lock(os.getenv('FRI_DATA', '/data')):
        Fri().run(os.environ['DISCORD_TOKEN'], log_handler=None)  # configure() already set up logging.


if __name__ == '__main__':
    main()
