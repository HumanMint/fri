"""Discord controls for the call contract. No voice receive or controller integration."""

import asyncio
import logging
import time
import uuid

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
            await call.brief_message.edit(view=self)
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

    @discord.ui.button(label='hang up', style=discord.ButtonStyle.danger,
                       custom_id='jpcall:hang_up')
    async def hang_up(self, interaction, button):
        if not self.call.requester(interaction):
            await tell(interaction, 'the person who asked can hang up.')
            return
        if self.call.session is None or self.call.phase != 'active':
            await tell(interaction, 'the call is already closing or still connecting.')
            return
        self.disable()
        await interaction.response.defer()
        await self.call.stop('requested')


class AnswerModal(discord.ui.Modal, title='answer for the call'):
    answer = discord.ui.TextInput(label='your answer', max_length=1000,
                                 custom_id='jpcall:answer_text')

    def __init__(self, view):
        # discord.py stores modals globally by custom_id. Each opening needs its
        # own identity so one member's submission/timeout cannot remove another's.
        super().__init__(timeout=max(1, view.deadline - time.monotonic()),
                         custom_id=f'jpcall:submit:{uuid.uuid4().hex}')
        self.question_view = view

    async def on_submit(self, interaction):
        await self.question_view.submit(interaction, str(self.answer))

    async def on_error(self, interaction, error):
        LOG.warning('jpcall answer failed: %s', type(error).__name__)
        await tell(interaction, 'that answer did not go through. try again.')


class QuestionView(Controls):
    def __init__(self, call, event):
        super().__init__(timeout=max(0.1, event.deadline_s))
        self.call, self.question_id = call, event.question_id
        self.deadline = time.monotonic() + event.deadline_s
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
        if not self.call.in_channel(interaction):
            await tell(interaction, 'answers come from people in this voice channel.')
            return False
        return True

    async def free_text(self, interaction):
        if self.closed or time.monotonic() >= self.deadline:
            await tell(interaction, 'that question is closed.')
            return
        await interaction.response.send_modal(AnswerModal(self))

    async def submit(self, interaction, answer):
        if not self.call.in_channel(interaction):
            await tell(interaction, 'answers come from people in this voice channel.')
            return
        if not answer.strip():
            await tell(interaction, 'i need a few words to pass along.')
            return
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            if (self.closed or time.monotonic() >= self.deadline or self.call.phase == 'ended'
                    or not self.call.in_channel(interaction)):
                await tell(interaction, 'that question is closed.')
                return
            if self.call.session is None:
                await tell(interaction, 'still connecting. try that answer again in a moment.')
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
        self.phase = 'planning'
        self._events_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._watchdog = None
        self._audio_monitor = None
        self._operation = None

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
        return await self.text_channel.send(content, allowed_mentions=NO_MENTIONS, **kwargs)

    async def edit(self, message, content):
        await message.edit(content=content, allowed_mentions=NO_MENTIONS)

    async def edit_view(self, message, view):
        if message is not None:
            try:
                await message.edit(view=view)
            except discord.HTTPException as exc:
                LOG.warning('jpcall control edit failed: HTTP %s', exc.status)

    async def start(self, interaction):
        if not self.requester(interaction) or not self.in_channel(interaction):
            raise CallRejected('the requester needs to stay in the voice channel.')
        if interaction.guild.voice_client is not None:
            raise CallRejected('i am already using voice here.')
        self.audio = CallAudioSource()
        self.pager = TranscriptPager(self.send, self.edit)
        self.cost = CostSummary(self.send, self.edit)
        def capture_voice(client, channel):
            # Keep the client even if the handshake raises or is cancelled.
            self.voice = CallVoiceClient(client, channel)
            return self.voice

        self.voice = await self.voice_channel.connect(cls=capture_voice, self_deaf=True)
        self.voice.track_speaking()
        await wait_audio_ready(self.voice, diagnostic=self.audio if self.owner.audio_check else None)
        if not self.requester(interaction) or not self.in_channel(interaction):
            raise CallRejected('the requester left before i could call.')
        self.hangup = HangupView(self)
        self.hangup_message = await self.send('connecting the call.', view=self.hangup)
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
        session = await self.owner.engine.start(self.brief, self.request, self.audio, self)
        self.session = session
        if self.phase == 'ended':
            # start() may emit a terminal event before returning the session.
            await session.hang_up('closed_during_start')
        else:
            self.phase = 'active'

    async def watchdog(self):
        await asyncio.sleep(self.owner.limits.max_call_s)
        await self.stop('time_limit')

    async def stop(self, reason):
        if self.phase == 'ended':
            return
        self.phase = 'ended'
        # Start the phone hangup immediately; Discord edits may be rate limited.
        hangup = (asyncio.create_task(self.session.hang_up(reason))
                  if self.session is not None else None)
        operation = self._operation
        if operation is not None and operation is not asyncio.current_task():
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
                if release:
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
        """Contract EventSink: UI failures must never propagate into the phone engine."""
        if event.request_id != self.request.request_id:
            return
        async with self._events_lock:
            try:
                if isinstance(event, UsageUpdated):
                    if self.cost is not None:
                        await self.cost.emit(event)
                    if self.pager is not None and self.phase == 'ended':
                        await self.pager.flush()
                    return
                if isinstance(event, CallFinished):
                    await self.cleanup()
                    await self.pager.close()
                    await self.cost.emit(event)
                    return
                if isinstance(event, (Utterance, Translation, QuestionClosed)):
                    await self.pager.emit(event)
                    if isinstance(event, QuestionClosed) and event.question_id in self.questions:
                        await self.questions[event.question_id].close()
                    return
                if self.phase == 'ended':
                    return
                if isinstance(event, StatusChanged) and event.state in (
                        CallState.ENDED, CallState.FAILED):
                    await self.pager.emit(event)
                    await self.cleanup()
                    await self.pager.close()
                    return
                await self.pager.emit(event)
                if isinstance(event, QuestionAsked):
                    view = QuestionView(self, event)
                    self.questions[event.question_id] = view
                    options = ''.join(f'\n{i}. {text}'
                                      for i, text in enumerate(event.options, 1))
                    pages = chunks(f'a question for the group · {event.text_en}{options}')
                    for index, content in enumerate(pages):
                        message = await self.send(content, view=view if index == len(pages) - 1
                                                  else None)
                    view.message = message
            except Exception as exc:
                LOG.warning('jpcall event display failed: %s', type(exc).__name__)


class DiscordCalls:
    def __init__(self, client, engine, guild_ids, limits, *, audio_check=False):
        self.client, self.engine = client, engine
        self.audio_check = audio_check
        self.guild_ids = frozenset(guild_ids)
        self.limits = limits
        self.active = {}

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
            text = (f'{brief.summary_en}\n\nopener · {brief.opener_ja}\n\n'
                    f'share · {"; ".join(brief.share) or "nothing extra"}\n'
                    f'never · {"; ".join(brief.never) or "no extra limits"}')
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

    async def close(self):
        await asyncio.gather(*(call.stop('shutdown') for call in list(self.active.values())))


def register(tree, client, engine, guild_ids, *, limits: Limits | None = None, audio_check=False):
    """Register guild commands; callers supply Limits explicitly. Default is deny-all."""
    guild_ids = tuple(guild_ids)
    if not guild_ids:
        raise ValueError('at least one guild is required')
    calls = DiscordCalls(client, engine, guild_ids, limits if limits is not None else Limits(),
                         audio_check=audio_check)

    @app_commands.command(name='jpcall', description='ask fri to phone a japanese business')
    @app_commands.guild_only()
    @app_commands.describe(number='phone number, including +81', goal='what you want to find out',
                           share='facts i may share, one per line', never='limits, one per line')
    async def command(interaction: discord.Interaction, number: str, goal: str,
                      share: str | None = None, never: str | None = None):
        await calls.invoke(interaction, number, goal, share, never)

    tree.add_command(command, guilds=[discord.Object(id=guild) for guild in guild_ids])
    return calls
