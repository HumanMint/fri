"""Bounded conversational tools. Discord text never selects authority or scope."""

import asyncio
import datetime as dt
import json
import time

import discord

from . import feedback as commands
from .fri_agent_model import ATTENTION, DIAGNOSIS
from .fri_context import discord_source
from .fri_conversation import PERSISTENT_MODEL_WRITES
from .fri_errors import ControllerRejected
from .fri_history_search import READ_ONLY_AFTER_HISTORY, HistorySearch
from .fri_history_search import requested as history_requested
from .fri_pending import Clarification, completion, research_query
from .fri_web import lookup
from .gene_controls import COMMANDS, arguments, grounded, music_control_intent, own_words, requested
from .privacy import clean
from .web_policy import public_query

CATCH_UP_MESSAGES, CATCH_UP_SECONDS = 100, 12 * 3600
CATCH_UP_CHARS = 20000  # Serialized rows, so emoji-heavy chat can't blow up the prompt.
RETURN_SECONDS = 5 * 60  # The speaker's own recent messages ("yo", "back") are part of coming back.
CATCH_UP = ('Only when the speaker asks what they missed: other members\' text in that channel since '
            'the speaker\'s last message there, up to 100 messages or 12 hours. No images.')


class Actions:
    def __init__(self, agent, user, channel, public):
        self.agent, self.fri = agent, agent.fri
        self.user, self.channel, self.public = user, channel, public
        self.guild = agent.channel_guild(channel) if public else self.fri.guild_id
        self.owner = user == self.fri.owner_id and not public
        self.sources, self.revisions, self.overview = set(), {}, None
        self.message, self.model, self.effort = '', '', ''
        self.event, self.scope, self.revision = None, {}, 0
        self.web_research = []
        self.settings_version = None
        self.private_evidence = False
        self.pending = None
        self.conversation = {}
        self.research = None
        self.search_count = 0
        self.image_context = {}
        self.media_allowed = False
        self.invitation = None
        self.turn_context = None
        self.caught_up = None
        from .fri_image_share import ImageShare
        self.image_share = ImageShare(self)
        self.history = HistorySearch(self)

    def command_requested(self, action):
        if not music_control_intent(self.message, action.get('action'),
                                  music_context=getattr(self, 'music_context', False)):
            return False
        continuation_only = getattr(self, 'continuation_only', None)
        if continuation_only and action.get('action') != continuation_only:
            return False
        if not continuation_only and requested(self.message, action.get('action')):
            return True
        if self.burst_permission(action):
            return True
        return bool(self.event and completion(self.pending, self.message, action.get('action'),
            action.get('argument'), discord_source(self.event).model_dump(),
            str(self.guild), self.scope.get('conversation_id', ''), str(self.user), self.revision,
            str(getattr(getattr(self.fri, 'user', None), 'id', ''))))

    def observe(self, scope, user, revision):
        # Keep the first revision read this turn: a later read must not relabel earlier evidence,
        # so valid() still notices the change before anything is delivered.
        self.revisions.setdefault((scope, user), revision)

    def derived(self):
        """Other members' words are in this turn's context, so they could reach a write."""
        from .fri import turn_provenance
        if self.turn_context is None:
            return False
        try:
            return bool(turn_provenance(self.turn_context, self.user, self))
        except commands.UserFeedbackCheckFailure:
            return True  # Someone's privacy changed mid-turn; the reply itself will be refused.

    def burst_permission(self, action):
        rows = self.conversation.get('message_burst', [])
        if not self.event or len(rows) < 2:
            return None
        from .fri_conversation import burst_continuation
        return burst_continuation(rows[-2], discord_source(self.event), clean(self.event.content, 4000),
            action.get('action'), action.get('argument'), self.revision,
            str(getattr(getattr(self.fri, 'user', None), 'id', '')))

    async def pending_unchanged(self):
        if not self.pending or not self.event:
            return False
        source = self.pending['source']
        if (source['user_id'] != str(self.user) or source['channel_id'] != str(self.event.channel.id)
                or self.pending['revision'] != self.revision):
            return False
        context = await self.fri.api('POST', '/chat/context', self.scope)
        if context.get('revision') != self.revision or context.get('pending') != self.pending:
            return False
        original = await self.event.channel.fetch_message(int(source['id']))
        return (original.author.id == self.user and not original.webhook_id and
                clean(original.content, 4000) == self.pending['request_text'])

    async def channels(self, fresh=False):
        member = await self.agent.member(self.user, fresh=fresh, guild_id=self.guild)
        if not member or not self.agent.config['enabled']:
            return {}
        ids = [str(self.channel)] if self.public else list(dict.fromkeys(
            self.agent.state['history_channels'] + self.agent.state['channels'] + [str(self.fri.workshop_id)]))
        result = {}
        for channel_id in ids[:10]:
            if not channel_id.isdigit():
                continue
            channel = self.fri.get_channel(int(channel_id))
            if (not channel or getattr(getattr(channel, 'guild', None), 'id', None) != self.guild or
                    (not self.agent.allowed(channel) and str(channel.id) not in self.agent.state['history_channels']) or getattr(channel, 'nsfw', False) or
                    (isinstance(channel, discord.Thread) and (channel.is_private() or getattr(channel.parent, 'nsfw', True)))):
                continue
            permissions = channel.permissions_for(member)
            if permissions.view_channel and permissions.read_message_history:
                result[str(channel.id)] = channel
        return result

    async def available(self):
        if self.invitation and self.invitation.outside:
            if not await self.invitation.current():
                return {}
            actions = {}
            if self.agent.state.get('web_search_enabled'):
                actions['web_search'] = {'description': 'Look up current public facts with sources. '
                    'query must be a short exact public question from the current message. '
                    'Never export nearby chat, captions, images, member details or private context.'}
            if self.web_research:
                actions['web_sources'] = {'description': 'Show retained public sources in this conversation. '
                    'query="" means latest; otherwise use an exact saved query. No new search.'}
            return actions
        channels = await self.channels()
        if not channels:
            return {}
        actions = {'read_recent': {'channels': [{'channel_id': k, 'name': clean(v.name, 60)}
                                                for k, v in channels.items()],
                    'default_channel_id': next(iter(channels)), 'limit': 20, 'max_age_days': 7,
                    'description': 'Read one small recent text batch when asked. No images or saved personal notes.',
                    'windows': {'recent': 'the read described above', 'since_my_last_message': CATCH_UP}}}
        if self.invitation:
            actions['read_recent'].update(limit=12, max_age_days=30/(24*60),
                description='Read up to twelve preceding same-channel messages from the last thirty minutes '
                'when this mention needs nearby conversation or meme context. Can inspect one attributed '
                'small still-image post, up to two attachments. This read disables persistent writes '
                'and external image selection for this turn; complete current requests need no read.')
        if isinstance(self.event, discord.Message) and history_requested(self.message):
            actions['history_search'] = self.history.descriptor(channels)
        if self.owner:
            self.settings_version = self.agent.state['version']
            actions['preview_voice'] = {'description': 'Prepare an owner-only voice/personality settings preview. '
                'No coding request; nothing is saved until Apply. voice_notes is the complete replacement style preference.',
                'current_voice_notes': self.agent.config['voice_notes'], 'max_characters': 1500}
            actions['learning_overview'] = {'description': 'Read the latest learning reports and summaries. channel_id="".'}
            actions['run_learning'] = {'description': 'Run one learning pass only when explicitly requested, within the existing budget. channel_id="".'}
        if self.agent.state.get('web_search_enabled'):
            actions['web_search'] = {'description': 'Look up current public facts with live web access and sources. '
                'query is a short exact public question from the current message or pending web topic. '
                'Include what the user wants to know, not just a generic keyword. The isolated researcher can rewrite queries.'}
            from .fri_image_share import descriptor
            from .fri_image_share import requested as image_requested
            if (self.media_allowed and not self.private_evidence and isinstance(self.event, discord.Message)
                    and image_requested(self.message)):
                actions['image_search'] = descriptor()
        if self.web_research:
            actions['web_sources'] = {'description': 'Show sources from saved public research in this conversation. '
                'query="" means latest; otherwise use an exact saved query. No new search.'}
        if self.guild == self.fri.guild_id and isinstance(self.event, discord.Message):
            actions['gene_diagnose'] = {'description': 'Read independent Gene heartbeat/command receipts and '
                'investigate a reported failure. Works when Gene commands are unavailable. channel_id="". '
                'Luna triages; Sol xhigh diagnoses. Existing-feature faults may enter the owner-authorized '
                'verification/repair pipeline; a report alone never approves code or deployment.'}
            catalog = await self.fri.api('GET', '/controls/catalog')
            offered = {key: COMMANDS[key][2] for key in catalog.get('actions', []) if key in COMMANDS}
            if offered:
                actions['gene_command'] = {'commands': offered, 'description': 'Use an existing Gene command now, '
                    'as this member with normal permissions. No owner development approval is needed. '
                    'Requires an explicit current request, not an example or capability question. '
                    'action is a listed command; argument is a song, numeric volume, or empty.'}
        from .fri_workspace import available
        actions.update(available(self))
        from .fri_social import available as social_available
        actions.update(social_available(self))
        if isinstance(self.event, discord.Message):
            goals = [k for k in ('play', 'volume') if k in actions.get('gene_command', {}).get('commands', {})
                     and requested(self.message, k)]
            if 'web_search' in actions and not self.pending:
                goals.append('web')
            if goals:
                actions['clarify'] = {'goals': goals, 'description': 'Ask ONE necessary question and hold this request for 5 minutes. '
                    'For play/volume, topic="". For web, topic is the original public QUESTION as an exact current-message excerpt. '
                    'question is the short reply shown to the member. Do not ask again when their answer resolves it.'}
        if self.private_evidence:
            for kind in PERSISTENT_MODEL_WRITES:
                actions.pop(kind, None)
            if 'clarify' in actions:
                goals = [goal for goal in actions['clarify']['goals'] if goal in ('play', 'volume')]
                if goals:
                    actions['clarify'] = {**actions['clarify'], 'goals': goals,
                        'description': 'Ask for the song or numeric volume explicitly requested in the current member message. '
                        'The harness supplies a fixed question; no referenced text may be retained. topic="".'}
                else:
                    actions.pop('clarify')
        return actions

    def scoped_overview(self, overview, channels):
        # The owner still needs current channel access. Reports without a channel only describe failures.
        return {**overview,
            'reports': [r for r in overview['reports'] if not r.get('channel_id') or r['channel_id'] in channels],
            'summaries': [r for r in overview['summaries'] if r['channel_id'] in channels],
            'historical_notes': [r for r in overview.get('historical_notes', []) if r['channel_id'] in channels],
            'history_channels': [c for c in overview['history_channels'] if c in channels]}

    async def execute(self, action):
        if self.invitation:
            await self.invitation.require_current()
            if self.invitation.outside:
                return await self.invited_web(action)
        if self.private_evidence and action.get('kind') in PERSISTENT_MODEL_WRITES:
            return {'blocked': 'Read member evidence cannot be copied into a persistent draft, post or setting. '
                    'Provide the request in a fresh message.'}
        if self.conversation and (self.conversation.get('message_burst') or self.conversation.get('reply_target')):
            from .fri_conversation import unchanged
            if not await unchanged(self.agent, self.event, self.conversation):
                return {'blocked': 'The conversation evidence changed. Send a fresh request.'}
        from .fri_images import require_current
        await require_current(self.image_context, self.event)
        channels = await self.channels(fresh=True)
        kind = action.get('kind')
        if not channels:
            return {'blocked': 'No approved channels are currently readable.'}
        if self.history.pages and kind not in READ_ONLY_AFTER_HISTORY:
            return {'blocked': 'Archive evidence cannot authorize another action. Ask for that action in a fresh message.'}
        if kind == 'history_search':
            return await self.history.execute(action, channels)
        from .fri_social import KINDS as SOCIAL_KINDS
        from .fri_social import execute as social_execute
        if kind in SOCIAL_KINDS:
            return await social_execute(self, action)
        from .fri_workspace import KINDS, execute
        if kind in KINDS:
            return await execute(self, action)
        if kind == 'preview_voice':
            if not self.owner or self.settings_version is None:
                return {'blocked': 'Voice settings are available only to the verified owner in private.'}
            notes = action.get('voice_notes')
            if not isinstance(notes, str) or not notes.strip() or len(notes) > 1500 or clean(notes, 1500) != notes:
                return {'blocked': 'Keep voice preferences within 1500 characters without secrets.'}
            state = await self.fri.api('GET', '/agent/state')
            if state['version'] != self.settings_version:
                self.agent.state = state
                return {'blocked': 'Settings changed while preparing this preview. Ask again for a fresh preview.'}
            return {'kind': kind, 'settings_preview': {'changes': {'voice_notes': notes},
                    'version': state['version']}}
        if kind == 'web_sources':
            matches = [r for r in self.web_research if not action.get('query') or r['query'] == action['query']]
            return {'kind': kind, **matches[0]} if matches else {'blocked': 'That search is no longer retained here. Ask for a fresh lookup.'}
        if kind == 'image_search':
            return await self.image_share.execute(action.get('query'))
        if kind == 'gene_command':
            return await self.control(action, channels)
        if kind == 'gene_diagnose':
            return await self.diagnose(channels)
        if kind == 'clarify':
            value = Clarification.model_validate({k: v for k, v in action.items() if k != 'kind'}).model_dump()
            if self.private_evidence:
                if value['goal'] not in ('play', 'volume') or value['topic'] or not requested(self.message, value['goal']):
                    return {'blocked': 'Read member evidence cannot be persisted in a clarification.'}
                original = await self.event.channel.fetch_message(self.event.id)
                if (original.author.id != self.user or original.webhook_id or original.content != self.event.content):
                    return {'blocked': 'Your current request changed. Send a fresh complete request.'}
                value['question'] = ('what song do you want?' if value['goal'] == 'play'
                                     else 'what volume from 0 to 100?')
            if (value['goal'] == 'web' and public_query(value['topic'], self.message) or
                    value['goal'] in ('play', 'volume') and requested(self.message, value['goal']) and not value['topic']):
                return {'clarification': value}
            return {'blocked': 'That question cannot extend an earlier command. Ask for a fresh complete request.'}
        if kind == 'web_refine':
            if (not self.agent.state.get('web_search_enabled') or not self.research or
                    self.search_count != 1 or action.get('query') not in ('', self.research['query'])):
                return {'blocked': 'There is no current public lookup to refine.'}
            self.search_count += 1
            result, counters, status = await lookup(self.fri.chat, self.research['query'], self.model, self.effort,
                                                   previous=self.research)
            await self.agent.record('web_search', self.model, self.effort, counters, status)
            if status == 'completed' and result.get('answer') and result.get('sources'):
                result['query'] = self.research['query']
            return result
        if kind == 'web_search':
            query = research_query(action.get('query'), self.message, self.pending)
            if not self.agent.state.get('web_search_enabled') or not query or self.search_count:
                return {'blocked': 'Provide a short public topic in the current message; private context cannot be sent to web search.'}
            if not public_query(query, self.message) and not await self.pending_unchanged():
                return {'blocked': 'The earlier question changed. Send the public topic again.'}
            if not public_query(query, self.message):
                # Validate provenance before searching; a later command or timeout can
                # consume the pending question without losing the completed research.
                await self.fri.api('POST', '/chat/research/start', {**self.scope,
                    'revision': self.revision, 'source': discord_source(self.event).model_dump(),
                    'input': self.message, 'query': query})
            self.search_count += 1
            result, counters, status = await lookup(self.fri.chat, query, self.model, self.effort)
            await self.agent.record('web_search', self.model, self.effort, counters, status)
            if status == 'completed' and result.get('answer') and result.get('sources'):
                result['query'] = query
                self.research = {k: result[k] for k in ('query', 'answer', 'sources', 'checked_at')}
            return result
        if kind in ('learning_overview', 'run_learning'):
            self.private_evidence = True
            if not self.owner:
                return {'blocked': 'Learning administration is available only to the verified owner in private.'}
            receipt = None
            if kind == 'run_learning':
                if not set(self.agent.state['history_channels']) <= set(channels):
                    return {'blocked': 'Some configured learning channels are not currently readable.'}
                claim = await self.fri.api('POST', '/agent/learning/request', self.agent.identity(self.user))
                if 'blocked' in claim:
                    return claim
                receipt = await self.agent.learn(claim)
            overview = await self.fri.api('POST', '/agent/learning/overview', self.agent.identity(self.user))
            self.overview = self.scoped_overview(overview, channels)
            self.sources.update(self.overview['history_channels'])
            return {'kind': kind, 'completion': receipt, 'evidence': self.overview}
        if kind != 'read_recent':
            return {'blocked': 'That action is not available.'}
        self.private_evidence = True
        channel_id = action.get('channel_id') or next(iter(channels))
        if channel_id not in channels:
            return {'blocked': 'That channel is outside this conversation’s readable scope.'}
        catch_up = action.get('window') == 'since_my_last_message'
        if self.invitation and not catch_up:
            return await self.invited_context(channel_id)
        channel = channels[channel_id]
        self.sources.add(channel_id)
        scope = getattr(channel, 'parent_id', None) or channel.id
        after = time.time()-min(7, self.agent.config['history_days'])*86400
        # A catch-up's window hangs off the request, so a slow model call can't move where they left.
        asked = time.time()
        if catch_up and isinstance(getattr(self.event, 'id', None), int):  # A message or a panel interaction.
            asked = discord.utils.snowflake_time(self.event.id).timestamp()
        if catch_up:
            after = max(after, asked-CATCH_UP_SECONDS)
        if channel.id == self.agent.dedicated:
            after = max(after, self.agent.state.get('started', time.time()))
        options = {'limit': 20, 'after': dt.datetime.fromtimestamp(after, dt.timezone.utc), 'oldest_first': False}
        if catch_up:
            options['limit'] = CATCH_UP_MESSAGES
            if isinstance(getattr(self.event, 'id', None), int):
                options['before'] = discord.Object(id=self.event.id)
        # A catch-up stops at the speaker's last message before anyone else spoke. Messages
        # they sent just before asking, or that only bots answered, are them coming back, not leaving.
        returned, others, left, full = asked-RETURN_SECONDS, False, None, False
        records, scanned, profiles, shown, size = [], 0, {}, {}, 0
        async for message in channel.history(**options):
            scanned += 1
            if message.author.bot or message.webhook_id:
                continue
            user = message.author.id
            if catch_up and user == self.user:
                if others and message.created_at.timestamp() < returned:
                    left = message
                    break
                continue
            others = True
            if not message.content.strip():
                continue
            if user not in profiles:
                profiles[user] = await self.agent.profile(scope, user)
            profile = profiles[user]
            if not profile['enabled']:
                continue
            if catch_up:  # Up to 100 rows, so drop the links and keep each line short.
                row = {'user_id': user, 'name': clean(message.author.display_name, 60),
                       'at': message.created_at.isoformat(timespec='minutes'), 'text': clean(message.content, 300)}
                size += len(json.dumps(row))
                if size > CATCH_UP_CHARS:
                    full = True
                    break
                self.observe(scope, user, profile['revision'])
                shown[message.id] = (user, message.content, message.edited_at)
                records.append(row)
                continue
            self.observe(scope, user, profile['revision'])
            records.append({'source': message.jump_url, 'user_id': user, 'name': clean(message.author.display_name, 60),
                            'at': message.created_at.isoformat(), 'text': clean(message.content, 500)})
        result = {'kind': kind, 'channel_id': channel_id, 'channel_name': clean(channel.name, 60),
                  'scanned': scanned, 'messages': list(reversed(records)), 'lookback_floor': after,
                  'images_inspected': False, 'personal_notes_saved': 0}
        if catch_up:
            self.caught_up = {'channel': channel, 'options': options, 'shown': shown}
            result.update(window='since_my_last_message',
                your_last_message_at=left.created_at.isoformat(timespec='minutes') if left else None,
                stopped_at='your_last_message' if left else 'size_limit' if full else
                    'message_limit' if scanned >= CATCH_UP_MESSAGES else 'time_limit')
        return result

    async def caught_up_unchanged(self):
        """Every message a catch-up showed must still exist unedited, like an invitation's sources."""
        if not self.caught_up:
            return True
        read, shown = self.caught_up, self.caught_up['shown']
        latest = {message.id: (message.author.id, message.content, message.edited_at)
                  async for message in read['channel'].history(**read['options']) if message.id in shown}
        return latest == shown

    async def invited_context(self, channel_id):
        """An explicit configured-channel read introduces ephemeral evidence only now."""
        from .fri_images import attach
        invitation = self.invitation
        if self.turn_context is None or str(channel_id) != str(self.event.channel.id):
            return {'blocked': 'Nearby mention context stays in this exact message channel.'}
        await invitation.collect()
        context = self.turn_context
        context.update(invitation.context())
        await attach(context, self.event, self.message, source=invitation.image, invitation=invitation)
        self.image_context = {key: context[key] for key in ('image_sources', 'image_event', 'image_invitation') if key in context}
        return {'kind': 'read_recent', 'channel_id': str(channel_id), 'messages': invitation.rows,
                'images_inspected': bool(context.get('images')), 'personal_notes_saved': 0,
                'ambiguous_context_images': invitation.ambiguous_images}

    async def control(self, action, channels):
        if self.guild != self.fri.guild_id or not isinstance(self.event, discord.Message):
            return {'blocked': 'Ask in Gene’s server or your member DM; Lab cannot control another server.'}
        verb, argument = action.get('action'), action.get('argument')
        try:
            arguments(verb, argument)
        except ValueError as exc:
            return {'blocked': str(exc)}
        if not self.command_requested(action):
            return {'blocked': 'That music request expired or needs a fresh instruction. Ask me to play it with the song included.'}
        continued = not requested(self.message, verb)
        burst = self.burst_permission(action) if continued else None
        if continued and not burst and not await self.pending_unchanged():
            return {'blocked': 'The earlier music request changed. Ask again with the song included.'}
        # A command receipt outlives erasure and returns as context, so it may hold only the
        # member's own words. A 0-100 volume carries none of anyone's, except from read evidence.
        if argument and not continued and not own_words(argument, self.message) and (
                self.private_evidence or (verb == 'play' and self.derived())):
            return {'blocked': 'Name the song or volume in your own message; words from other people or earlier '
                    'context cannot be saved in a Gene command.'}
        original = await self.event.channel.fetch_message(self.event.id)
        if original.author.id != self.user or original.webhook_id or original.content != self.event.content:
            return {'blocked': 'The original request changed. Send a fresh request.'}
        current = await self.fri.api('POST', '/chat/context', self.scope)
        if current['revision'] != self.revision:
            return {'blocked': 'The conversation was cleared before Gene could start.'}
        # A DM has no text destination of its own. Use the explicit utility channel,
        # never the development workshop or an arbitrary readable channel.
        target = str(self.channel)
        if not self.public:
            target = (await self.fri.api('GET', '/controls/catalog')).get('dm_channel_id', '')
            if target not in channels:
                return {'blocked': 'The configured bot commands channel is not accessible. Ask there instead.'}
        from .fri_context import discord_source
        self.sources.add(target)
        submitted = await self.fri.api('POST', '/controls', {**self.scope, 'source': discord_source(self.event).model_dump(),
            'target_channel_id': target, 'request_text': clean(self.event.content, 4000),
            'action': verb, 'argument': argument, 'revision': self.revision,
            'continuation_id': (burst or self.pending)['source']['id'] if continued else '',
            'burst_origin': self.conversation['message_burst'][-2] if burst else None, 'grounded': True,
            'addressed_bot_id': str(getattr(getattr(self.fri, 'user', None), 'id', ''))})
        result = None
        try:
            async with asyncio.timeout(120):
                while True:
                    await asyncio.sleep(2)
                    result = await self.fri.api('POST', '/controls/'+submitted['id']+'/result', self.scope)
                    if result.get('done'):
                        break
        except TimeoutError:
            pass  # Stop waiting, not the underlying Red operation; never resubmit automatically.
        if not result or not result.get('done'):
            result = {'uncertain': 'Gene has not confirmed completion. Check the player before retrying.'}
        if result.get('uncertain') and not getattr(self, 'operational', False):
            # The command-wait deadline must not cancel diagnosis and cause a second model run.
            result['diagnostic'] = await self.diagnose(channels)
        return {'kind': 'gene_command', 'action': verb, **result}

    async def diagnose(self, channels):
        if self.guild != self.fri.guild_id or not isinstance(self.event, discord.Message):
            return {'blocked': 'Gene diagnosis is available only in its server or verified member DMs.'}
        channels = await self.channels(fresh=True)
        if not channels:
            return {'blocked': 'No approved channels are currently readable.'}
        original = await self.event.channel.fetch_message(self.event.id)
        if original.author.id != self.user or original.webhook_id or original.content != self.event.content:
            return {'blocked': 'The source message changed; ask again for a fresh check.'}
        current = await self.fri.api('POST', '/chat/context', self.scope)
        if current['revision'] != self.revision:
            return {'blocked': 'The conversation was cleared before diagnosis.'}
        try:
            health = await self.fri.api('POST', '/controls/health', self.scope)
            recent = await self.fri.api('POST', '/controls/recent', self.scope)
        except commands.UserFeedbackCheckFailure:
            return {'uncertain': 'The independent health check is unavailable; no repair has started.'}
        receipts = grounded([r for r in recent.get('receipts', []) if r['channel_id'] in channels])
        self.sources.update(r['channel_id'] for r in receipts)
        repairs = []
        auto_available = self.agent.state.get('auto_repairs_enabled', False)
        if auto_available:
            try:
                repairs = (await self.fri.api('POST', '/repairs/recent', self.scope))['repairs']
            except commands.UserFeedbackCheckFailure:
                auto_available = False  # Preserve read-only diagnosis and its normal review preview.
        evidence = {'source_id': str(self.event.id), 'report': self.message, 'health': health, 'receipts': receipts,
                    'previous_repairs': repairs}
        gate, _, state = await self.agent.run('attention',
            'Triage a current member report about Gene. Choose investigate for a reported failure, no reply, '
            'unconfirmed command, or unhealthy runtime that warrants diagnosis. Choose quiet for unrelated chat '
            'or a normal status question with healthy evidence. Do not diagnose or approve repairs. '
            'Use the supplied source_id.', evidence, ATTENTION)
        result = {'kind': 'gene_diagnose', 'health': health, 'receipts': receipts, 'triage_status': state,
                  'previous_repairs': repairs}
        if state != 'completed' or gate.get('action') != 'investigate' or gate.get('source_id') != str(self.event.id):
            return result
        diagnosis, _, state = await self.agent.run('investigation',
            'Read-only diagnosis of a reported Gene failure. Separate current heartbeat/poll evidence from dated '
            'receipts and the member report. A fresh heartbeat is not proof of audible playback or command success. '
            'A restart does not identify its cause; no process logs or shell are available. Never invent a root cause. '
            'Give a brief evidence-based assessment and, when useful, a self-contained proposed repair request; '
            'otherwise proposed_request="". No coding or repair has started. Feature jobs can edit only cogs/, '
            'tests/features/, docs/features/. Flag infrastructure, dependencies and controller issues as maintainer '
            'work. An eligible existing-feature fault can be sent to independent verification; do not promise '
            'a fix or grant approval yourself. New features and protected changes need the owner. At most 2500 characters total.',
            evidence, DIAGNOSIS, model='gpt-6.1-sol', effort='xhigh')
        result.update(diagnosis_status=state, diagnosis_model='gpt-6.1-sol', diagnosis_effort='xhigh')
        if state == 'completed':
            result.update(assessment=clean(diagnosis.get('summary', ''), 1500),
                          proposed_request=clean(diagnosis.get('proposed_request', ''), 4000))
            if result['proposed_request'] and auto_available:
                latest = await self.event.channel.fetch_message(self.event.id)
                if (not await self.valid() or latest.author.id != self.user or latest.webhook_id or
                        latest.content != self.event.content):
                    return {'blocked': 'Access or the report changed before repair verification.'}
                try:
                    result['repair'] = await self.fri.api('POST', '/repairs', {**self.scope,
                        'source': discord_source(latest).model_dump(), 'revision': self.revision,
                        'body': clean('Reported failure (untrusted):\n'+self.message+'\n\nAssessment:\n'+
                                      result['assessment']+'\n\nProposed investigation:\n'+result['proposed_request'], 5500)})
                except ControllerRejected as exc:
                    result['repair'] = {'accepted': False, 'reason': clean(str(exc), 500)}
                except commands.UserFeedbackCheckFailure:
                    result['repair'] = {'accepted': False, 'uncertain': 'The controller did not confirm whether verification was queued. Check current requests before resubmitting.'}
                if result['repair'].get('accepted') or result['repair'].get('uncertain'):
                    result['proposed_request'] = ''
        return result

    async def valid(self):
        """Fail closed if access or source privacy changed while the model was answering."""
        try:
            if self.invitation:
                if not await self.invitation.current():
                    return False
                if self.invitation.outside:
                    return True
            if self.conversation and (self.conversation.get('message_burst') or self.conversation.get('reply_target')):
                from .fri_conversation import unchanged
                if not await unchanged(self.agent, self.event, self.conversation):
                    return False
            from .fri_images import unchanged
            if not await unchanged(self.image_context, self.event):
                return False
            channels = await self.channels(fresh=True)
            if not channels or not self.sources <= set(channels):
                return False
            if not await self.history.valid(channels):
                return False
            if not await self.caught_up_unchanged():
                return False
            for (scope, user), revision in self.revisions.items():
                profile = await self.agent.profile(scope, user)
                if not profile['enabled'] or profile['revision'] != revision:
                    return False
            if self.overview is not None:
                latest = self.scoped_overview(await self.fri.api('POST', '/agent/learning/overview',
                    self.agent.identity(self.user)), channels)
                # A new report may arrive during inference; all evidence used must still exist unchanged.
                for key in ('reports', 'summaries', 'historical_notes'):
                    if any(row not in latest[key] for row in self.overview[key]):
                        return False
            return True
        except (commands.UserFeedbackCheckFailure, discord.HTTPException, TimeoutError):
            return False

    async def invited_web(self, action):
        """Only public excerpts of this event can leave an outside invitation."""
        kind = action.get('kind')
        if kind == 'web_sources':
            matches = [r for r in self.web_research if not action.get('query') or r['query'] == action['query']]
            return {'kind': kind, **matches[0]} if matches else {'blocked': 'That search is no longer retained here. Ask for a fresh lookup.'}
        if not self.agent.state.get('web_search_enabled'):
            return {'blocked': 'Public lookup is currently disabled.'}
        if kind == 'web_search':
            query = action.get('query')
            if self.search_count or not public_query(query, self.message):
                return {'blocked': 'Provide a short public question in this message; nearby chat cannot be sent to search.'}
            await self.fri.api('POST', '/chat/research/start', {**self.scope, 'revision': self.revision,
                'input': self.message, 'query': query})
            self.search_count = 1
            result, counters, status = await lookup(self.fri.chat, query, self.model, self.effort)
        elif kind == 'web_refine':
            if not self.research or self.search_count != 1 or action.get('query') not in ('', self.research['query']):
                return {'blocked': 'There is no current public lookup to refine.'}
            self.search_count = 2
            query = self.research['query']
            result, counters, status = await lookup(self.fri.chat, query, self.model, self.effort, previous=self.research)
        else:
            return {'blocked': 'That action is unavailable in a one-message invitation.'}
        await self.agent.record('web_search', self.model, self.effort, counters, status)
        await self.invitation.require_current()
        if status == 'completed' and result.get('answer') and result.get('sources'):
            result['query'] = query
            self.research = {k: result[k] for k in ('query', 'answer', 'sources', 'checked_at')}
        return result
