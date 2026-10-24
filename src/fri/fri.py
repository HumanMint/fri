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
from .fri_errors import ControllerRejected
from .fri_memory import describe_memory, memory_intent
from .fri_notices import ReplyInProgress, WaitingNotices, notice_text
from .fri_panel import DraftView, HomeView, authorized, card, reply, sessions
from .fri_protocol import PROTOCOL, compatible
from .fri_reports import ReportButton, report_component
from .fri_requests import request_dm_component
from .fri_responses import quick_closure
from .fri_reviews import ReviewButton, notify_owner
from .fri_usage import describe_usage, usage_requested
from .panels import defer_panel
from .privacy import clean
from .reviews import owner_dm_component
from .subscription import Subscription, private_json

LOG = logging.getLogger('fri')
NO_MENTIONS = discord.AllowedMentions.none()
FIRST_MEMORY = ('\n\n-# i kept a small preference from this. ask “what do you remember about me?” '
                'or say “forget me” any time.')
MAX_PROVENANCE = 200
UNATTRIBUTED = ('Earlier reply-context check. Fetch current Discord evidence for follow-up details; '
                'no quoted findings are retained here.')
UNAVAILABLE = '[an earlier fri reply whose sources are no longer available]'
# Reads of other channels, archives, learning reports or diagnostics stay neutral breadcrumbs.
PRIVATE_READS = frozenset({'read_recent', 'history_search', 'learning_overview', 'run_learning', 'gene_diagnose'})


def private_read(receipts):
    """Includes a diagnosis nested in an uncertain Gene command receipt."""
    return any(r.get('kind') in PRIVATE_READS or r.get('diagnostic') or (r.get('gene_result') or {}).get('diagnostic')
               for r in receipts)


def turn_provenance(context, speaker, actions=None):
    """Other members whose messages shaped this turn, with the privacy revision observed.

    Every context source carrying another member's words contributes: the live channel view
    (including Fri's own replies), saved history, a pending clarification, the channel summary,
    reply targets (Fri's resolved to their sources), nearby invited context and read evidence.
    """
    seen = {}
    inherited = [item for turn in context.get('turns', []) for item in turn.get('provenance', [])]
    inherited += (context.get('pending') or {}).get('provenance', [])
    inherited += context.get('summary_provenance', []) + context.get('reference_provenance', [])
    for item in inherited:
        seen.setdefault(str(item['user_id']), item['revision'])
    for row in context.get('recent', []):
        if row.get('gene_error'):
            continue
        if row.get('fri'):  # Fri's own earlier reply carries the members it was built from.
            for item in row.get('provenance', []):
                seen.setdefault(str(item['user_id']), item['revision'])
        else:
            seen.setdefault(str(row['user_id']), row.get('revision', 0))
    for row in context.get('reply_target', []) + context.get('invited_context', []):
        if row.get('privacy_revision') is not None:
            seen.setdefault(str(row['source']['user_id']), row['privacy_revision'])
    for (_, user), revision in (getattr(actions, 'revisions', None) or {}).items():
        seen.setdefault(str(user), revision)
    seen.pop(str(speaker), None)
    return [{'user_id': user, 'revision': int(revision)} for user, revision in sorted(seen.items())]


class FriTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        return await authorized(self.client, interaction)

    async def on_error(self, interaction, error):
        LOG.warning('Fri command failed: %s', type(error).__name__)
        await reply(self.client, interaction, 'That action did not complete. Please try again.')


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
        self.busy = set()
        self.waiting_notices = WaitingNotices(self.busy)
        self.usage_lock = asyncio.Lock()
        self.chat_slots = asyncio.Semaphore(2)
        self.session = None
        self.agent = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        if not compatible(await self.api('GET', '/fri/contract')):
            raise RuntimeError('unsupported gene controller contract; fri did not start')
        from .fri_agent import Agent
        self.agent = Agent(self)
        await self.agent.start()
        self.add_view(HomeView(self))
        from .fri_social import SocialButton
        from .fri_workspace import PostButton
        self.add_dynamic_items(ReviewButton, ReportButton, PostButton, SocialButton)

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
            if (not allowed and not owner_dm_component(interaction, getattr(self.user, 'id', None), self.owner_id)
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
                data = await response.json()
                if 400 <= response.status < 500:
                    raise ControllerRejected(str(data.get('detail', 'Controller rejected this request')))
                if response.status >= 400:
                    raise commands.UserFeedbackCheckFailure(str(data.get('detail', 'Controller rejected this request')))
                return data
        except (aiohttp.ClientError, TimeoutError):
            raise commands.UserFeedbackCheckFailure('The workshop controller is temporarily unavailable.') from None

    async def flush_usage(self):
        async with self.usage_lock:
            for path in sorted((self.data / 'usage').glob('*.json')):
                try:
                    report = json.loads(path.read_text())
                except FileNotFoundError:
                    continue  # An isolated evaluation process may have delivered this receipt.
                await self.api('POST', '/chat/usage/report', report)
                path.unlink(missing_ok=True)

    async def chat_once(self, text, context):
        result = await self.chat.respond(text, context)
        attempt = uuid.uuid4().hex
        private_json(self.data / 'usage' / (attempt + '.json'), {
            'attempt_id': attempt, 'model': context['model'], 'effort': context['effort'],
            'status': result.status, 'counters': result.counters})
        try:
            await self.flush_usage()
        except commands.UserFeedbackCheckFailure:
            LOG.warning('Usage is saved locally; controller delivery will retry')
        if result.error or result.status != 'completed':
            raise commands.UserFeedbackCheckFailure(result.error or 'Fri could not complete that reply. Try again shortly.')
        return result

    async def talk(self, guild_id, channel_id, user_id, text, visibility, display_name='', *, event=None, burst=(), inspect_images=True, invitation=None):
        if invitation and invitation.outside:
            return await self.invited_talk(invitation, text)
        source = discord_source(event, text, getattr(self.user, 'id', 0)).check(guild_id, f'{visibility}:{channel_id}', user_id)
        channel = self.get_channel(channel_id) if self.agent else None
        source_channel = (getattr(channel, 'parent_id', None) or channel_id) if visibility == 'public' and self.agent else self.workshop_id
        scope = {'guild_id': str(guild_id), 'user_id': str(user_id), 'channel_id': str(source_channel),
                 'conversation_id': f'{visibility}:{channel_id}'}
        # Privacy controls must work while inference is busy or the subscription is down.
        intent = memory_intent(text)
        if intent == 'show':
            context = await self.api('POST', '/chat/context', scope)
            profile = context['personal_memory']
            profile['notes'] = [{**note, 'evidence': discord.utils.escape_markdown(note['evidence'])}
                                for note in profile['notes']]
            return ChatResult(reply=describe_memory(profile), status='completed', memory_scope=scope)
        if intent in ('disable', 'enable'):
            await self.api('POST', '/chat/memory/control', {**scope, 'action': intent})
            if self.agent:
                self.agent.forget(user_id)
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
            raise commands.UserFeedbackCheckFailure('Keep each message between 1 and 4000 characters.')
        self.busy.add(user_id)
        try:
            async with self.chat_slots:
                context = await self.api('POST', '/chat/context', scope)
                conversation = {}
                if self.agent and source.kind == 'discord_message':
                    from .fri_conversation import evidence
                    try:
                        conversation = await evidence(self.agent, event, burst)
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
                    usage = await self.api('POST', '/agent/usage',
                                           {'guild_id': str(guild_id), 'user_id': str(user_id)})
                    report = describe_usage(usage, by_model='by model' in ' '.join(text.casefold().split()))
                    result = self.stage_turn(scope, source, text, context, ChatResult(reply=report, status='completed'),
                                             model='local-usage-report', derived=False)
                    await self.api('POST', '/chat/turn/check', result.turn)  # An erasure mid-turn stops it.
                    return result
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
                    admission = (await invitation.admit() if invitation else
                        await self.api('POST', '/agent/admit', {**self.agent.identity(user_id, guild_id),
                            'channel_id': str(source_channel),
                            'space': 'dm' if visibility == 'private' else 'public'}))
                    if not admission['allowed']:
                        minutes = max(1, int((admission['until']-time.time()+59)//60))
                        notice = (f'bro im cooked rn, gotta recharge my social battery and save daniel’s ai wallet lol. '
                                  f'back in about {minutes} mins') if admission.get('notice') else ''
                        return ChatResult(reply=notice, status='cooldown')
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
                if actions:
                    actions.image_context = {key: context[key] for key in ('image_sources', 'image_event', 'image_invitation') if key in context}
                if actions and guild_id == self.guild_id:
                    recent = await self.api('POST', '/controls/recent', scope)
                    readable = await actions.channels(fresh=True)
                    context['gene_receipts'] = [r for r in recent.get('receipts', []) if r['channel_id'] in readable]
                    actions.sources.update(r['channel_id'] for r in context['gene_receipts'])
                    from .gene_controls import current_music_context
                    actions.music_context = current_music_context(context['gene_receipts'])
                text = clean(text, 4000)
                result = await self.chat_once(text, context)
                if invitation:
                    await invitation.require_current()
                if conversation and (conversation.get('message_burst') or conversation.get('reply_target')):
                    from .fri_conversation import unchanged
                    if not await unchanged(self.agent, event, conversation):
                        raise commands.UserFeedbackCheckFailure('That conversation changed while I was replying. Ask again for a fresh reply.')
                from .fri_images import require_current as require_current_images
                await require_current_images(context, event)
                if guild_id != self.guild_id:
                    result.draft = None
                if actions and actions.private_evidence:
                    result.draft, result.notes = None, []
                if result.action:
                    if not actions or result.action['kind'] not in context['actions']:
                        raise commands.UserFeedbackCheckFailure('That action is not available here.')
                    from .fri_runner import run
                    result, receipts, research = await run(self, actions, text, context, result)
                    if actions.private_evidence:
                        result.draft, result.notes = None, []
                    if invitation:
                        await invitation.require_current()
                    if context.get('force_text_reply'):
                        from .fri_mentions import force_text
                        result = force_text(result)
                    await require_current_images(context, event)
                    receipt = receipts[-1]
                    action_kind = receipt['kind']
                    from .fri_outcomes import unrecorded
                    try:
                        current = await self.api('POST', '/chat/context', scope)
                    except (commands.UserFeedbackCheckFailure, TimeoutError):
                        return ChatResult(reply=unrecorded(receipts, reason='I couldn’t recheck conversation access to show the details.'), status='completed')
                    if current['revision'] != context['revision'] or not await actions.valid():
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
                    if private and not (result.clarification or result.settings_preview or result.post_preview):
                        # Other channels' messages, archives and reports never become saved chat text.
                        checked_channel = receipt.get('channel_id', '')
                        stored = f'Earlier check: {action_kind}' + (f' in channel {checked_channel}' if checked_channel else '')
                        stored += '. Fetch current evidence for follow-up details; no findings are retained here.'
                    result.turn = {**scope, 'revision': context['revision'], 'source': source.model_dump(),
                        'input': clean(event.content, 4000) if result.clarification or context.get('message_burst') else text,
                        'reply': stored, 'model': context['model'], 'effort': context['effort'], 'notes': [],
                        'web_receipt': research, 'clarification': result.clarification,
                        'burst': context.get('message_burst', []),
                        'allow_closing': result.allow_closing and not bool(result.draft or result.settings_preview or
                                                                         result.post_preview or result.clarification),
                        # A fixed harness question over referenced evidence derives nothing from it.
                        'provenance': [] if result.clarification and actions.private_evidence
                                      else turn_provenance(context, user_id, actions),
                        'verbatim': stored == result.reply}
                    if len(result.turn['provenance']) > MAX_PROVENANCE:
                        return ChatResult(reply=unrecorded(receipts, reason='too many people were in that context to '
                                                           'track safely.'), status='completed')
                    if context.get('unattributed_reference'):
                        result.turn.update(reply=UNATTRIBUTED, verbatim=False)
                    elif not private:
                        result.echo = self.echo(scope, context, result.turn['provenance'], stored)
                    try:  # The save's privacy fence, applied before anything is shown.
                        await self.api('POST', '/chat/turn/check', result.turn)
                    except commands.UserFeedbackCheckFailure:
                        return ChatResult(reply=unrecorded(receipts), status='completed')
                    return result
                if context.get('gene_receipts') and not await actions.valid():
                    raise commands.UserFeedbackCheckFailure('Access changed during that check. Ask again for a fresh reply.')
                result = self.stage_turn(scope, source, text, context, result, actions=actions)
                # The save's privacy fence runs before delivery; the save itself waits for delivery.
                await self.api('POST', '/chat/turn/check', result.turn)
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
        if unattributed:  # A referenced Fri message whose sources are unknown is never retained.
            reply, result.notes = UNATTRIBUTED, []
        result.turn = {**scope, 'revision': context['revision'], 'source': source.model_dump(), 'input': text,
            'reply': reply, 'response_kind': result.response_kind, 'reaction': result.reaction,
            'allow_closing': result.allow_closing, 'burst': context.get('message_burst', []),
            'model': model or context['model'], 'effort': '' if model else context['effort'],
            'notes': result.notes, 'input_tokens': 0 if model else result.counters.get('input_tokens'),
            'output_tokens': 0 if model else result.counters.get('output_tokens'), 'provenance': provenance,
            'verbatim': result.response_kind == 'reply' and reply == result.reply}
        if derived and result.response_kind == 'reply' and not unattributed:
            result.echo = self.echo(scope, context, provenance, reply)
        return result

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
        if unresolved:
            # Fail closed before inference: the model never sees text whose sources erased or were never
            # attributable, and nothing derived is retained. A copy keeps the integrity checks intact.
            context['unattributed_reference'] = True
            context['reply_target'] = [{**row, 'text': UNAVAILABLE} if row['source']['id'] in unresolved else row
                                       for row in context['reply_target']]
        context['reference_provenance'] = [item for mid in own for item in found.get(mid, {}).get('provenance', [])]

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
        try:
            saved = await self.api('POST', '/chat/turn', payload)
        except (commands.UserFeedbackCheckFailure, ControllerRejected, TimeoutError):
            LOG.warning('Delivered turn was not recorded')  # Includes privacy changes mid-reply.
            return None
        if saved.get('first_memory') and sent is not None and payload.get('response_kind', 'reply') == 'reply':
            try:
                await sent.edit(content=(sent.content or '')+FIRST_MEMORY)
            except discord.HTTPException:
                LOG.info('First-memory notice was not added')
        return saved

    @staticmethod
    def mention_cooldown(admission):
        minutes = max(1, int((admission.get('until', time.time())-time.time()+59)//60))
        return ChatResult(reply=(f'bro im cooked rn, gotta recharge my social battery and save daniel’s ai wallet lol. '
                                f'back in about {minutes} mins') if admission.get('notice') else '', status='cooldown')

    async def invited_talk(self, invitation, text):
        """Outside the ambient scope, one ping has only conversation and public research."""
        from .fri_context import addressed_content
        from .fri_images import attach, require_current
        from .fri_mentions import force_text
        event, user = invitation.event, invitation.event.author.id
        expected = addressed_content(event.content, self.user.id) or 'hey'
        if text != expected or not 1 <= len(text) <= 4000:
            raise commands.UserFeedbackCheckFailure('Keep each message between 1 and 4000 characters.')
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
                scope = invitation.granted_scope()
                context = await self.api('POST', '/chat/context', scope)
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
                actions.image_context = {k: context[k] for k in ('image_sources', 'image_event', 'image_invitation') if k in context}
                result = await self.chat_once(text, context)
                research = None
                if result.action:
                    if result.action['kind'] not in context['actions']:
                        raise commands.UserFeedbackCheckFailure('That action is not available here.')
                    from .fri_runner import run
                    result, receipts, research = await run(self, actions, text, context, result)
                    receipt = research or receipts[-1]
                    if receipt.get('sources'):
                        from .fri_web import cite_reply, links_requested
                        result.reply = cite_reply(result.reply, receipt,
                            include_sources=receipts[-1]['kind'] == 'web_sources' or links_requested(text))
                result = force_text(result)
                result.draft, result.notes = None, []
                await invitation.require_current()
                await require_current(context, event)
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
            view = DraftView(self, interaction.user.id, result.draft) if result.draft else HomeView(self, interaction.user.id)
            if result.settings_preview:
                from .fri_agent import BehaviorPreview
                view = BehaviorPreview(self.agent, **result.settings_preview)
                await reply(self, interaction, view.content, attachments=[], view=view, verbatim=True)
            else:
                await reply(self, interaction, embed=card('Fri', result.reply), view=view)
            # A newer panel interaction can supersede this one; record only what was displayed.
            saved = await self.record_turn(result) if sessions(self).displayed(interaction) else None
            if saved and saved.get('first_memory') and not result.settings_preview:
                await reply(self, interaction, embed=card('Fri', result.reply+FIRST_MEMORY), view=view)
        except commands.UserFeedbackCheckFailure as exc:
            await reply(self, interaction, str(exc), view=HomeView(self, interaction.user.id))
        await self.waiting_notices.clear(interaction.user.id)

    async def on_message(self, message):
        if self.agent:
            try:
                await self.agent.handle(message)
            except (commands.UserFeedbackCheckFailure, discord.HTTPException):
                LOG.warning('Fri interaction could not complete')
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
            view = DraftView(self, message.author.id, result.draft, public=True) if result.draft else None
            sent = await message.reply(result.reply, view=view, mention_author=False, allowed_mentions=NO_MENTIONS,
                                       suppress_embeds=True)
            await self.record_turn(result, sent)
            await self.waiting_notices.clear(message.author.id)
        except ReplyInProgress:
            await self.waiting_notices.send(message)
        except commands.UserFeedbackCheckFailure as exc:
            await message.reply(notice_text(str(exc))[:1800], mention_author=False, allowed_mentions=NO_MENTIONS)
            await self.waiting_notices.clear(message.author.id)

    @tasks.loop(seconds=15)
    async def owner_reviews(self):
        try:
            await self.flush_usage()
            await notify_owner(self)
        except (commands.UserFeedbackCheckFailure, aiohttp.ClientError, TimeoutError):
            LOG.warning('Owner notification or usage delivery pending')

    @owner_reviews.before_loop
    async def before_reviews(self):
        await self.wait_until_ready()

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
                'status': self.agent.heartbeat_status} if self.agent else None,
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
        await sessions(self).close_all()
        if self.session:
            await self.session.close()
        await super().close()


def main():
    from .logging import install_secret_filter
    install_secret_filter()
    from .fri_instance import instance_lock
    with instance_lock(os.getenv('FRI_DATA', '/data')):
        Fri().run(os.environ['DISCORD_TOKEN'], log_level=logging.INFO)


if __name__ == '__main__':
    main()
