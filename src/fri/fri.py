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

    async def talk(self, guild_id, channel_id, user_id, text, visibility, display_name='', *, event=None, burst=(), inspect_images=True):
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
                context['source'] = source.evidence()
                context['acknowledgments_enabled'] = bool(self.agent and source.kind == 'discord_message')
                from .fri_images import select_source
                image_source = await select_source(event) if inspect_images else None
                if image_source is None and quick_closure(
                        text, {**context, 'recent': self.agent.messages(channel_id) if self.agent else []}):
                    result = ChatResult(status='completed', response_kind='react', reaction='👍')
                    return await self.save_response(scope, source, text, context, result, model='local-closing')
                if user_id == self.owner_id and guild_id == self.guild_id and usage_requested(text):
                    usage = await self.api('POST', '/agent/usage',
                                           {'guild_id': str(guild_id), 'user_id': str(user_id)})
                    report = describe_usage(usage, by_model='by model' in ' '.join(text.casefold().split()))
                    await self.api('POST', '/chat/turn', {**scope, 'revision': context['revision'],
                        'source': source.model_dump(), 'input': text, 'reply': report,
                        'model': 'local-usage-report', 'effort': '', 'notes': [],
                        'input_tokens': 0, 'output_tokens': 0})
                    return ChatResult(reply=report, status='completed')
                from .fri_operational import admit as admit_operation
                operation = await admit_operation(self, event, source, scope, text, context)
                if operation.result:
                    if operation.result.status == 'completed':
                        saved_result = (ChatResult(reply='Earlier check: gene_command. Fetch current evidence for follow-up details; no findings are retained here.', status='completed')
                                        if operation.tool_receipt else operation.result)
                        try:
                            await self.save_response(scope, source, text, context, saved_result, model='local-control')
                        except (commands.UserFeedbackCheckFailure, TimeoutError):
                            if not operation.tool_receipt:
                                raise
                            from .fri_outcomes import unrecorded
                            LOG.warning('Tool result preserved; chat recording unavailable')
                            return ChatResult(reply=unrecorded([operation.tool_receipt]), status='completed')
                    return operation.result
                if self.agent and self.agent.config['enabled'] and not operation.continuation:
                    admission = await self.api('POST', '/agent/admit', {**self.agent.identity(user_id, guild_id),
                        'channel_id': str(source_channel),
                        'space': 'dm' if visibility == 'private' else 'public'})
                    if not admission['allowed']:
                        minutes = max(1, int((admission['until']-time.time()+59)//60))
                        notice = (f'bro im cooked rn, gotta recharge my social battery and save daniel’s ai wallet lol. '
                                  f'back in about {minutes} mins') if admission.get('notice') else ''
                        return ChatResult(reply=notice, status='cooldown')
                if self.agent:
                    context.update(await self.agent.extra_context(channel_id, user_id, visibility == 'public'))
                actions = Actions(self.agent, user_id, channel_id, visibility == 'public') if self.agent else None
                if actions:
                    actions.event, actions.scope, actions.revision = event, scope, context['revision']
                    actions.message, actions.model, actions.effort = clean(text, 4000), context['model'], context['effort']
                    actions.web_research = context.get('web_research', [])
                    actions.pending = context.get('pending')
                    actions.continuation_only = actions.pending['goal'] if operation.continuation else None
                    actions.conversation = conversation
                    actions.private_evidence = bool(conversation.get('reply_target'))
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
                    await attach_images(context, event, text, source=image_source)
                if actions:
                    actions.image_context = {key: context[key] for key in ('image_sources', 'image_event') if key in context}
                if actions and guild_id == self.guild_id:
                    recent = await self.api('POST', '/controls/recent', scope)
                    readable = await actions.channels(fresh=True)
                    context['gene_receipts'] = [r for r in recent.get('receipts', []) if r['channel_id'] in readable]
                    actions.sources.update(r['channel_id'] for r in context['gene_receipts'])
                    from .gene_controls import current_music_context
                    actions.music_context = current_music_context(context['gene_receipts'])
                text = clean(text, 4000)
                result = await self.chat_once(text, context)
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
                    # Keep only a neutral breadcrumb so follow-ups know which evidence to fetch again.
                    # Retrieved messages/answers never outlive source erasure in someone else's chat.
                    checked_channel = receipt.get('channel_id', '')
                    breadcrumb = f'Earlier check: {action_kind}'
                    if checked_channel:
                        breadcrumb += f' in channel {checked_channel}'
                    breadcrumb += ('. Public research receipts are available separately for follow-ups.' if research or action_kind == 'web_sources'
                                   else '. Fetch current evidence for follow-up details; no findings are retained here.')
                    if result.clarification or ((result.settings_preview or result.post_preview) and not actions.private_evidence):
                        breadcrumb = result.reply
                    try:
                        await self.api('POST', '/chat/turn', {**scope, 'revision': context['revision'],
                            'source': source.model_dump(),
                            'input': clean(event.content, 4000) if result.clarification or context.get('message_burst') else text,
                            'reply': breadcrumb, 'model': context['model'],
                            'effort': context['effort'], 'notes': [], 'web_receipt': research, 'clarification': result.clarification,
                            'burst': context.get('message_burst', []),
                            'allow_closing': result.allow_closing and not bool(result.draft or result.settings_preview or
                                                                             result.post_preview or result.clarification)})
                    except (commands.UserFeedbackCheckFailure, TimeoutError):
                        LOG.warning('Tool result preserved; chat recording unavailable')
                        return ChatResult(reply=unrecorded(receipts), status='completed')
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
                    return result
                if context.get('gene_receipts') and not await actions.valid():
                    raise commands.UserFeedbackCheckFailure('Access changed during that check. Ask again for a fresh reply.')
                return await self.save_response(scope, source, text, context, result)
        finally:
            self.busy.discard(user_id)

    async def save_response(self, scope, source, text, context, result, *, model=None):
        if context.get('message_burst'):
            text = context['message_burst'][-1]['text']
        # A reply to someone else can quote their words. Keep the live answer on
        # Discord, but never preserve derived copies in this member's memory.
        referenced = bool(context.get('reply_target'))
        if referenced:
            result.draft, result.notes = None, []
        stored_reply = ('Earlier reply-context check. Fetch current Discord evidence for follow-up details; '
                        'no quoted findings are retained here.') if referenced else result.reply
        saved = await self.api('POST', '/chat/turn', {**scope, 'revision': context['revision'],
            'source': source.model_dump(), 'input': text, 'reply': stored_reply if result.response_kind == 'reply' else result.reply,
            'response_kind': result.response_kind, 'reaction': result.reaction,
            'allow_closing': result.allow_closing and not referenced,
            'burst': context.get('message_burst', []),
            'model': model or context['model'], 'effort': '' if model else context['effort'],
            'notes': result.notes, 'input_tokens': 0 if model else result.counters.get('input_tokens'),
            'output_tokens': 0 if model else result.counters.get('output_tokens')})
        if saved.get('first_memory') and result.response_kind == 'reply':
            result.reply += ('\n\n-# i kept a small preference from this. ask “what do you remember about me?” '
                             'or say “forget me” any time.')
        return result

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
            await message.reply(result.reply, view=view, mention_author=False, allowed_mentions=NO_MENTIONS,
                                suppress_embeds=True)
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
