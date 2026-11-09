"""Discord controls for the call contract. No voice receive or controller integration."""

import asyncio
import logging
import time
import uuid
import weakref
from collections import deque

import discord
from discord import app_commands

from .contract import (
    CallFinished,
    CallRejected,
    CallRequest,
    CallState,
    Limits,
    QuestionAsked,
    QuestionClosed,
    StatusChanged,
    Translation,
    UsageUpdated,
    Utterance,
)
from .discord_audio import CallAudioSource
from .discord_cost import CostSummary
from .discord_text import chunks
from .discord_transcript import TranscriptPager
from .discord_voice import CallVoiceClient, wait_audio_ready
from .numbers import normalize_number

LOG = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


async def tell(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


class Controls(discord.ui.View):
    def disable(self):
        for item in self.children:
            item.disabled = True
        self.stop()

    async def on_error(self, interaction, error, item):
        LOG.warning('jpcall control failed: %s', type(error).__name__)
        await tell(interaction, 'that did not go through. try again.')


class BriefView(Controls):
    def __init__(self, call):
        super().__init__(timeout=120)
        self.call = call

    async def interaction_check(self, interaction):
        if not self.call.requester(interaction):
            await tell(interaction, 'this one belongs to the person who asked.')
            return False
        return True

    @discord.ui.button(label='call', style=discord.ButtonStyle.primary, custom_id='jpcall:call')
    async def dial(self, interaction, button):
        call = self.call
        if call.phase != 'planned' or not call.requester(interaction):
            await tell(interaction, 'that brief is closed.')
            return
        if not call.in_channel(interaction):
            await tell(interaction, 'join the voice channel you asked from, then try again.')
            return
        call.phase = 'starting'  # Claim before the first await: duplicate clicks cannot dial twice.
        self.disable()
        call._operation = asyncio.current_task()
        try:
            await interaction.response.defer()
            call.hangup = HangupView(call)
            call.hangup_message = call.brief_message
            await call.brief_message.edit(view=call.hangup)
            if call.phase != 'ended':
                await call.start(interaction)
        except asyncio.CancelledError:
            await call.cleanup()
            raise
        except Exception as exc:
            await call.fail(exc, interaction)
        finally:
            call._operation = None

    @discord.ui.button(label='cancel', custom_id='jpcall:cancel')
    async def cancel(self, interaction, button):
        if self.call.phase != 'planned' or not self.call.requester(interaction):
            await tell(interaction, 'that brief is closed.')
            return
        self.call.phase = 'ended'
        self.disable()
        self.call.release()
        await interaction.response.edit_message(content='call cancelled.', embed=None, view=self)

    async def on_timeout(self):
        if self.call.phase == 'planned':
            self.call.phase = 'ended'
            self.call.release()
            self.disable()
            await self.call.edit_view(self.call.brief_message, self)


class HangupView(Controls):
    def __init__(self, call):
        super().__init__(timeout=None)
        self.call = call

    @discord.ui.button(label='stop', style=discord.ButtonStyle.danger,
                       custom_id='jpcall:hang_up')
    async def hang_up(self, interaction, button):
        if not self.call.requester(interaction):
            await tell(interaction, 'the person who asked can hang up.')
            return
        if self.call.phase not in ('starting', 'active'):
            await tell(interaction, 'the call is already closing.')
            return
        self.disable()
        # Revoke locally before even the interaction acknowledgement can block.
        self.call._stop_reason = 'requested'
        if self.call.audio is not None:
            self.call.audio.close()
        stopping = asyncio.create_task(self.call.stop('requested'))
        await interaction.response.defer()
        await stopping


class AnswerModal(discord.ui.Modal, title='answer for the call'):
    answer = discord.ui.TextInput(label='your answer', max_length=1000,
                                 custom_id='jpcall:answer_text')

    def __init__(self, view):
        # discord.py stores modals globally by custom_id. Each opening needs its
        # own identity so one member's submission/timeout cannot remove another's.
        super().__init__(timeout=max(0.001, view.deadline - time.monotonic()),
                         custom_id=f'jpcall:submit:{uuid.uuid4().hex}')
        self.question_view = weakref.ref(view)

    async def on_submit(self, interaction):
        view = self.question_view()
        if view is None:
            await tell(interaction, 'that question is closed.')
            return
        await view.submit(interaction, str(self.answer))

    async def on_error(self, interaction, error):
        LOG.warning('jpcall answer failed: %s', type(error).__name__)
        await tell(interaction, 'that answer did not go through. try again.')


class QuestionView(Controls):
    def __init__(self, call, event):
        deadline = (event.deadline_at if event.deadline_at is not None
                    else time.monotonic() + event.deadline_s)
        super().__init__(timeout=max(0.001, deadline - time.monotonic()))
        self.call, self.question_id = call, event.question_id
        self.deadline = deadline
        self.generation = event.consent_generation
        self.message = None
        self.closed = False
        self.token = uuid.uuid5(uuid.NAMESPACE_OID,
                               f'{call.request.request_id}:{event.question_id}').hex
        self._lock = asyncio.Lock()
        # Discord allows 25 components in a view; reserve one for free text.
        for index, option in enumerate(event.options[:24]):
            button = discord.ui.Button(label=chunks(option, 80)[0] if option else 'answer',
                                       custom_id=f'jpcall:answer:{self.token}:{index}')

            async def choose(interaction, answer=option):
                await self.submit(interaction, answer)

            button.callback = choose
            self.add_item(button)
        button = discord.ui.Button(label='write an answer', style=discord.ButtonStyle.primary,
                                   custom_id=f'jpcall:write:{self.token}')
        button.callback = self.free_text
        self.add_item(button)

    async def interaction_check(self, interaction):
        if not self.call.requester(interaction) or not self.call.in_channel(interaction):
            await tell(interaction, 'only the requester can answer; suggest it to them in chat.')
            return False
        return True

    async def free_text(self, interaction):
        if (self.closed or time.monotonic() >= self.deadline
                or not self.call.publication_allowed(self.generation)
                or not self.call.requester(interaction) or not self.call.in_channel(interaction)):
            await tell(interaction, 'that question is closed.')
            return
        await interaction.response.send_modal(AnswerModal(self))

    async def submit(self, interaction, answer):
        if not self.call.requester(interaction) or not self.call.in_channel(interaction):
            await tell(interaction, 'only the requester can answer; suggest it to them in chat.')
            return
        if not answer.strip():
            await tell(interaction, 'i need a few words to pass along.')
            return
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            if (self.closed or time.monotonic() >= self.deadline
                    or not self.call.publication_allowed(self.generation)
                    or not self.call.requester(interaction)
                    or not self.call.in_channel(interaction)):
                await tell(interaction, 'that question is closed.')
                return
            try:
                accepted = await self.call.session.answer(self.question_id, answer.strip(),
                                                          interaction.user.id)
            except Exception as exc:
                LOG.warning('jpcall answer rejected by engine: %s', type(exc).__name__)
                await tell(interaction, 'that answer did not go through. try again.')
                return
            await self.close()
            await tell(interaction, 'passed that along.' if accepted else 'that question is closed.')

    async def close(self):
        self.closed = True
        self.disable()
        await self.call.edit_view(self.message, self)

    async def on_timeout(self):
        await self.close()


class DiscordCall:
    def __init__(self, owner, request, voice_channel, text_channel):
        self.owner, self.request = owner, request
        self.voice_channel, self.text_channel = voice_channel, text_channel
        self.brief = self.brief_message = self.brief_view = None
        self.voice = self.session = self.audio = None
        self.pager = self.cost = None
        self.hangup = self.hangup_message = None
        self.questions = {}
        self.posts = set()  # IDs of this call's own posts: a reply to one is to the call, not to chat Fri.
        self.phase = 'planning'
        self._pending_events = deque()
        self._event_worker = None
        self._cleanup_lock = asyncio.Lock()
        self._watchdog = None
        self._audio_monitor = None
        self._operation = None
        self._stop_reason = None
        self._engine_starting = False
        self._event_generations = {}
        self._final = False  # The engine's terminal status arrived: no later transcript text.

    def generation(self):
        return getattr(self.session, 'consent_generation', None)

    def publication_allowed(self, generation):
        """Live call interaction: closed once the call ends or is stopped."""
        return (self.phase != 'ended' and self._stop_reason is None
                and self.text_allowed(generation))

    def text_allowed(self, generation):
        """Transcript text: only consent withdrawal hides it; an ordinary end or Stop does not."""
        return (self.session is not None and self.session.publication_allowed
                and generation is not None and generation == self.generation())

    def usage_sink(self):
        return self.cost.receipt.emit

    def requester(self, interaction):
        return (interaction.guild_id == self.request.guild_id
                and interaction.user.id == self.request.requester_id
                and interaction.user.id in self.owner.limits.allowed_user_ids)

    def in_channel(self, interaction):
        channel = getattr(getattr(interaction.user, 'voice', None), 'channel', None)
        return (interaction.guild_id == self.request.guild_id
                and getattr(channel, 'id', None) == self.request.voice_channel_id
                and not getattr(interaction.user, 'bot', False))

    def release(self):
        if self.owner.active.get(self.request.guild_id) is self:
            del self.owner.active[self.request.guild_id]

    async def send(self, content, **kwargs):
        message = await self.text_channel.send(content, allowed_mentions=NO_MENTIONS, **kwargs)
        self.posts.add(getattr(message, 'id', None))
        return message

    async def edit(self, message, content=discord.utils.MISSING, **kwargs):
        await message.edit(content=content, allowed_mentions=NO_MENTIONS, **kwargs)

    async def edit_view(self, message, view):
        if message is not None:
            try:
                await message.edit(view=view)
            except Exception as exc:
                LOG.warning('jpcall control edit failed: %s', type(exc).__name__)

    async def start(self, interaction):
        if not self.requester(interaction) or not self.in_channel(interaction):
            raise CallRejected('the requester needs to stay in the voice channel.')
        if interaction.guild.voice_client is not None:
            raise CallRejected('i am already using voice here.')
        self.audio = CallAudioSource()
        self.pager = TranscriptPager(self.send, self.edit, allowed=self.text_allowed)
        self.cost = CostSummary(self.send, self.edit,
                                http=self.text_channel._state.http,
                                channel_id=self.text_channel.id)
        def capture_voice(client, channel):
            # Keep the client even if the handshake raises or is cancelled.
            self.voice = CallVoiceClient(client, channel)
            return self.voice

        self.voice = await self.voice_channel.connect(cls=capture_voice, self_deaf=True)
        if self.phase == 'ended':
            await self.cleanup()
            return
        self.voice.track_speaking()
        await wait_audio_ready(self.voice, diagnostic=self.audio if self.owner.audio_check else None)
        if not self.requester(interaction) or not self.in_channel(interaction):
            raise CallRejected('the requester left before i could call.')
        loop = asyncio.get_running_loop()

        def player_ended(error):
            if error is not None:
                LOG.warning('jpcall audio player failed: %s', type(error).__name__)
            # Engine-owned close can precede its terminal event. Don't cancel
            # that event path just because the player consumed the final frame.
            if error is None and self.audio.closed:
                return
            if self.phase != 'ended':
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self.stop('voice_disconnected')))

        if (self.phase == 'ended' or not self.requester(interaction)
                or not self.in_channel(interaction)):
            raise CallRejected('the requester left before i could call.')
        self.voice.play(self.audio, after=player_ended)
        if self.owner.audio_check:
            self._audio_monitor = asyncio.create_task(self.voice.monitor_audio(self.audio))
        self._watchdog = asyncio.create_task(self.watchdog())
        self._engine_starting = True
        try:
            session = await self.owner.engine.start(self.brief, self.request, self.audio, self)
            self.session = session
            if self.phase == 'ended':
                await session.hang_up(self._stop_reason or 'closed_during_start')
            else:
                self.phase = 'active'
        finally:
            self._engine_starting = False
            if self.phase == 'ended':
                self.release()

    async def watchdog(self):
        # Backstop only: the engine closes by its own attempt deadline, then cleans up.
        await asyncio.sleep(self.owner.limits.attempt_s + 30)
        await self.stop('time_limit')

    async def stop(self, reason):
        if self.phase == 'ended':
            return
        self.phase = 'ended'
        self._stop_reason = reason
        if self.audio is not None:
            self.audio.close()
        # Start the phone hangup immediately; Discord edits may be rate limited.
        hangup = (asyncio.create_task(self.session.hang_up(reason))
                  if self.session is not None else None)
        operation = self._operation
        if (operation is not None and operation is not asyncio.current_task()
                and not self._engine_starting):
            operation.cancel()
        await self.cleanup(release=False)
        if operation is not None and operation is not asyncio.current_task():
            try:
                await operation
            except asyncio.CancelledError:
                pass
        try:
            if hangup is not None:
                await hangup
        except Exception as exc:
            LOG.warning('jpcall hang up failed: %s', type(exc).__name__)
        finally:
            if not self._engine_starting:
                self.release()
            if self.pager is not None:
                await self.pager.close()

    async def cleanup(self, *, release=True):
        self.phase = 'ended'
        async with self._cleanup_lock:
            try:
                watchdog, self._watchdog = self._watchdog, None
                if watchdog is not None and watchdog is not asyncio.current_task():
                    watchdog.cancel()
                if self.audio is not None:
                    self.audio.close()
                if self.voice is not None:
                    self.voice.stop()
                    await self.voice.disconnect(force=True)
                    self.voice = None
            except Exception as exc:
                LOG.warning('jpcall voice cleanup failed: %s', type(exc).__name__)
            finally:
                if self._audio_monitor is not None:
                    self._audio_monitor.cancel()
                    try:
                        await self._audio_monitor
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        LOG.warning('jpcall audio monitor failed: %s', type(exc).__name__)
                    self._audio_monitor = None
                if release and not self._engine_starting:
                    self.release()
            if self.brief_view is not None:
                self.brief_view.disable()
                await self.edit_view(self.brief_message, self.brief_view)
            if self.hangup is not None:
                self.hangup.disable()
                await self.edit_view(self.hangup_message, self.hangup)
            for view in self.questions.values():
                await view.close()

    async def fail(self, error, interaction):
        LOG.warning('jpcall start failed: %s', type(error).__name__)
        await self.stop('start_failed')
        try:
            if isinstance(error, CallRejected):
                await tell(interaction, str(error))
            else:
                await self.send('could not connect that call.')
        except discord.HTTPException:
            pass

    async def emit(self, event):
        """Admit without awaiting Discord: the engine puts a five-second deadline here."""
        try:
            if event.request_id != self.request.request_id:
                return
            if isinstance(event, CallFinished) or (isinstance(event, StatusChanged)
                    and event.state in (CallState.ENDED, CallState.FAILED)):
                self.phase = 'ended'
                self._final = True
                if self.audio is not None:
                    self.audio.close()
            generation = (event.consent_generation if isinstance(
                event, (Utterance, Translation, QuestionAsked)) else None)
            if isinstance(event, Utterance):
                if self._final or not self.text_allowed(generation):
                    return
                self._event_generations[event.item_id] = generation
            elif isinstance(event, Translation):
                if (self._final or not self.text_allowed(generation)
                        or self._event_generations.get(event.item_id) != generation):
                    return
            elif isinstance(event, QuestionAsked) and not self.publication_allowed(generation):
                return
            self._pending_events.append((event, generation))
            if self._event_worker is None or self._event_worker.done():
                self._event_worker = asyncio.create_task(self._drain_events())
                self.owner._event_tasks.add(self._event_worker)
                self._event_worker.add_done_callback(self.owner._event_tasks.discard)
        except Exception as exc:
            LOG.warning('jpcall event admission failed: %s %s',
                        type(event).__name__, type(exc).__name__)

    async def _drain_events(self):
        while self._pending_events:
            event, generation = self._pending_events.popleft()
            try:
                await self._display_event(event, generation)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                LOG.warning('jpcall event display failed: %s CancelledError', type(event).__name__)
            except Exception as exc:
                LOG.warning('jpcall event display failed: %s %s',
                            type(event).__name__, type(exc).__name__)

    async def flush_events(self):
        while self._event_worker is not None and not self._event_worker.done():
            await asyncio.shield(self._event_worker)

    async def _display_event(self, event, generation=None):
        if isinstance(event, UsageUpdated):
            if self.cost is not None:
                await self.cost.emit(event)
            if self.pager is not None and self.phase == 'ended':
                await self.pager.flush()
            return
        if isinstance(event, CallFinished):
            # A transcript/control failure must not suppress the final report.
            try:
                await self.cleanup()
                if self.pager is not None:
                    await self.pager.close(final=True)
            finally:
                if self.cost is not None:
                    await self.cost.emit(event)
            return
        if isinstance(event, (Utterance, Translation)):
            if self.text_allowed(generation):
                await self.pager.emit(event)
            return
        if isinstance(event, QuestionClosed):
            await self.pager.emit(event)
            if event.question_id in self.questions:
                await self.questions[event.question_id].close()
            return
        if isinstance(event, StatusChanged) and event.state in (CallState.ENDED, CallState.FAILED):
            # Progress stays in the transcript; terminal status belongs to the single final report.
            if self.cost is not None:
                await self.cost.emit(event)
            await self.cleanup()
            if self.pager is not None:
                await self.pager.close()
            return
        if self.phase == 'ended':
            return
        if isinstance(event, QuestionAsked) and (
                not self.publication_allowed(generation)
                or (event.deadline_at is not None and time.monotonic() >= event.deadline_at)):
            return
        await self.pager.emit(event)
        if isinstance(event, QuestionAsked):
            view = QuestionView(self, event)
            self.questions[event.question_id] = view
            options = ''.join(f'\n{i}. {text}' for i, text in enumerate(event.options, 1))
            question = (f'{event.text_original}\n↳ {event.text_en}'
                        if event.text_original else event.text_en)
            pages = chunks(f'a question for the requester · {question}{options}\n'
                           'answer with the buttons; chat isn\'t passed on. information only; no commitments.')
            for index, content in enumerate(pages):
                if (not self.publication_allowed(generation)
                        or time.monotonic() >= view.deadline):
                    await view.close()
                    return
                message = await self.send(content, view=view if index == len(pages) - 1 else None)
            view.message = message
            if not self.publication_allowed(generation) or time.monotonic() >= view.deadline:
                await view.close()


class DiscordCalls:
    def __init__(self, client, engine, guild_ids, limits, *, audio_check=False):
        self.client, self.engine = client, engine
        self.audio_check = audio_check
        self.guild_ids = frozenset(guild_ids)
        self.limits = limits
        self.active = {}
        self._event_tasks = set()

    def posted(self):
        """The running calls' own posts, which chat Fri leaves to the call (see Agent.call_posts)."""
        return frozenset(i for call in self.active.values() for i in call.posts)

    def posting_in(self, channel_id):
        return any(getattr(call.text_channel, 'id', None) == channel_id for call in self.active.values())

    def accepts(self, interaction):
        if interaction.guild_id not in self.guild_ids:
            return False
        data = interaction.data or {}
        if interaction.type == discord.InteractionType.application_command:
            return data.get('name') == 'jpcall'
        return (interaction.type in (discord.InteractionType.component,
                                     discord.InteractionType.modal_submit)
                and str(data.get('custom_id', '')).startswith('jpcall:'))

    async def invoke(self, interaction, number, goal, share=None, never=None):
        if (interaction.guild_id not in self.guild_ids
                or interaction.user.id not in self.limits.allowed_user_ids):
            await tell(interaction, 'this command is not available to you.')
            return
        channel = getattr(getattr(interaction.user, 'voice', None), 'channel', None)
        if not isinstance(channel, discord.VoiceChannel):
            await tell(interaction, 'join a voice channel first.')
            return
        if interaction.guild_id in self.active:
            await tell(interaction, 'one call at a time here.')
            return
        try:
            number = normalize_number(number, self.limits.test_numbers)
        except CallRejected as exc:
            await tell(interaction, str(exc))
            return
        request = CallRequest(uuid.uuid4().hex, interaction.guild_id, channel.id,
                              interaction.user.id, number, goal,
                              tuple(line.strip() for line in (share or '').splitlines() if line.strip()),
                              tuple(line.strip() for line in (never or '').splitlines() if line.strip()))
        call = DiscordCall(self, request, channel, interaction.channel)
        self.active[interaction.guild_id] = call
        call._operation = asyncio.current_task()
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            call.brief = await self.engine.plan(request)
            if call.phase == 'ended':
                return
            if call.brief.request_id != request.request_id:
                raise CallRejected('that brief belongs to a different call.')
            call.phase = 'planned'
            call.brief_view = BriefView(call)
            brief = call.brief
            language = getattr(getattr(getattr(self.engine, 'config', None), 'language', None),
                               'name', getattr(self.engine, 'language_name', 'Japanese'))
            text = (f'destination · {request.number_e164}\nlanguage · {language}\n\n'
                    f'opener · {brief.opener_ja}\n\ngoal · {request.goal}\n\n'
                    f'share · {"; ".join(request.share) or "nothing extra"}\n'
                    f'never · {"; ".join(request.never) or "no extra limits"}\n\n'
                    'information only · no bookings, payments, fees or cancellation terms.\n'
                    f'answers approved by · <@{request.requester_id}>\n'
                    'notice · AI assistant; group listens and text remains in this chat.\n'
                    f'limits · {self.limits.attempt_s:g}s call; '
                    f'{self.limits.max_hold_s:g}s per answer.\n'
                    'cost · metered estimate / pending; not a spending cap.')
            pages = chunks(text)
            for index, content in enumerate(pages):
                kwargs = dict(embed=discord.Embed(title='before i call', description=content),
                              view=call.brief_view if index == len(pages) - 1 else None)
                message = await call.send(None, **kwargs)
            call.brief_message = message
            try:
                await interaction.edit_original_response(content='brief is in this chat.')
            except discord.HTTPException as exc:
                LOG.warning('jpcall brief acknowledgment failed: HTTP %s', exc.status)
        except asyncio.CancelledError:
            await call.cleanup()
            raise
        except Exception as exc:
            call.phase = 'ended'
            call.release()
            LOG.warning('jpcall planning failed: %s', type(exc).__name__)
            text = str(exc) if isinstance(exc, CallRejected) else 'could not plan that call.'
            await tell(interaction, text)
        finally:
            call._operation = None

    async def flush_events(self):
        while self._event_tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._event_tasks)),
                                 return_exceptions=True)

    async def close(self):
        await asyncio.gather(*(call.stop('shutdown') for call in list(self.active.values())))
        await self.flush_events()


def register(tree, client, engine, guild_ids, *, limits: Limits | None = None, audio_check=False):
    """Register guild commands; callers supply Limits explicitly. Default is deny-all."""
    guild_ids = tuple(guild_ids)
    if not guild_ids:
        raise ValueError('at least one guild is required')
    calls = DiscordCalls(client, engine, guild_ids, limits if limits is not None else Limits(),
                         audio_check=audio_check)

    @app_commands.command(name='jpcall', description='ask fri to phone a business')
    @app_commands.guild_only()
    @app_commands.describe(number='phone number, including country code', goal='what you want to find out',
                           share='facts i may share, one per line', never='limits, one per line')
    async def command(interaction: discord.Interaction, number: str, goal: str,
                      share: str | None = None, never: str | None = None):
        await calls.invoke(interaction, number, goal, share, never)

    tree.add_command(command, guilds=[discord.Object(id=guild) for guild in guild_ids])
    return calls
