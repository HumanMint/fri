"""GA Realtime WebSocket bridge. No member microphone audio enters this client.

Protocol references checked 2026-11-04:
https://developers.openai.com/api/reference/resources/realtime/client-events
https://developers.openai.com/api/reference/resources/realtime/server-events
https://www.twilio.com/docs/voice/media-streams/websocket-messages
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

import aiohttp

from .config import JpcallConfig
from .contract import AudioSink, CallBrief, EventSink, Utterance
from .server import TwilioMedia, ulaw_to_pcm16
from .timeline import DebugTimeline
from .usage import UsageMeter

# Every Realtime event type lives here, including client commands.
EVENTS = {
    'session_created': 'session.created',
    'session_updated': 'session.updated',
    'session_update': 'session.update',
    'input_append': 'input_audio_buffer.append',
    'speech_started': 'input_audio_buffer.speech_started',
    'speech_stopped': 'input_audio_buffer.speech_stopped',
    'input_transcript': 'conversation.item.input_audio_transcription.completed',
    'input_transcript_failed': 'conversation.item.input_audio_transcription.failed',
    'output_audio': 'response.output_audio.delta',
    'output_audio_done': 'response.output_audio.done',
    'output_transcript': 'response.output_audio_transcript.done',
    'output_transcript_delta': 'response.output_audio_transcript.delta',
    'response_created': 'response.created',
    'response_done': 'response.done',
    'response_create': 'response.create',
    'response_cancel': 'response.cancel',
    'truncate': 'conversation.item.truncate',
    'item_create': 'conversation.item.create',
    'item_delete': 'conversation.item.delete',
    'function_arguments_done': 'response.function_call_arguments.done',
    'error': 'error',
}
REALTIME_URL = 'wss://api.openai.com/v1/realtime'
LOG = logging.getLogger(__name__)
FATAL_CODES = frozenset({'invalid_api_key', 'authentication_error', 'permission_denied',
                         'session_expired', 'session_not_found', 'invalid_session',
                         'model_not_found'})
ASR_TIMEOUT_S = 5
TRANSPORT_CLOSE_TIMEOUT_S = 1
BUSY_CODES = frozenset({'conversation_already_has_active_response', 'response_in_progress'})
ToolHook = Callable[[str, dict, str], Awaitable[None]]


class RealtimeError(RuntimeError):
    """Safe fixed error; API frames, URLs and credentials never enter the message."""


@dataclass
class _Playout:
    item_id: str
    content_index: int
    sent_bytes: int = 0
    played_bytes: int = 0
    started_at: float = 0
    response_id: str = ''
    generation: int = 0
    consent_generation: int | None = None


@dataclass
class _Command:
    number: str
    response: dict
    done: asyncio.Future = field(repr=False)
    generation: int = 0
    consent_generation: int | None = None
    opener: bool = False


class RealtimeClient:
    """A MediaHandler owned by one call's event loop.

    The caller owns AudioSink.close and post-call events. ``on_tool_call`` is
    invoked only for function items in a completed response (never interrupted
    argument deltas). It should schedule long-running group work and return.
    ``wait`` reports a failed receive task; media is closed immediately on failure.
    """

    def __init__(self, config: JpcallConfig, brief: CallBrief, audio: AudioSink,
                 events: EventSink, meter: UsageMeter, *,
                 session: aiohttp.ClientSession | None = None,
                 on_tool_call: ToolHook | None = None, tools: tuple[dict, ...] = (),
                 clock: Callable[[], float] = time.monotonic,
                 on_speech: Callable[[bool, str], None] | None = None,
                 goal: str = '', timeline: DebugTimeline | None = None,
                 on_asr_failure: Callable[[], Awaitable[None]] | None = None,
                 on_callee_response: Callable[[], None] | None = None,
                 publication_context: Callable[[], int | None] | None = None):
        self.config, self.brief = config, brief
        self.audio_sink, self.events, self.meter = audio, events, meter
        self._session = session
        self._owned = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._media: TwilioMedia | None = None
        self._task: asyncio.Task | None = None
        self._clock = clock
        self._answered_at = 0.0
        self._on_tool_call, self._tools = on_tool_call, tools
        self._on_speech = on_speech
        self._on_asr_failure = on_asr_failure
        self._on_callee_response = on_callee_response
        self._publication_context = publication_context
        self._goal = goal
        self.timeline = timeline or DebugTimeline(config.debug_timeline_path, clock=clock)
        self._recent_speech: deque[tuple[float, str, str]] = deque(maxlen=64)
        self._partial_transcripts: dict[str, str] = {}
        self._input_seen: set[str] = set()
        self._preopening_items: set[str] = set()
        self._turn_pending = False
        self._reply_needed = False
        self._playout: dict[tuple[str, int], _Playout] = {}
        self._generating: set[tuple[str, int]] = set()
        self._marks: dict[str, tuple[tuple[str, int], int]] = {}
        self._mark_seq = 0
        self._interrupted: set[str] = set()
        self._starts: dict[str, float] = {}
        self._input_context: dict[str, int | None] = {}
        self._response_context: dict[str, int | None] = {}
        self._ends: dict[str, float] = {}
        self._asr_timers: dict[str, asyncio.Task] = {}
        self._asr_failed_items: set[str] = set()
        self._asr_recovery: asyncio.Task | None = None
        self._asr_clarified = False
        self._asr_ok = asyncio.Event()
        self._utterances: set[tuple[str, str]] = set()
        self._tools_seen: set[str] = set()
        self._stopped = False
        self._media_closed = False
        self._output_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._response_lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._drained = asyncio.Event()
        self._drained.set()
        self._pending: _Command | None = None
        self._commands: dict[str, _Command] = {}
        self._active_responses: set[str] = set()
        self._response_seq = 0
        self._busy_unknown = False
        self._side_tasks: set[asyncio.Task] = set()
        self._transcripts: dict[tuple[str, int], str] = {}
        self._opener_requested = False
        self._conversation_open = False
        self._permission = False
        self._permission_generation = 0
        self._revocation: asyncio.Task | None = None
        self._response_commands: dict[str, str] = {}
        self._opener_responses: set[str] = set()
        self._completed_responses: set[str] = set()
        self._delivered_responses: set[str] = set()
        self._silenced = False
        self._allowed_responses: set[str] = set()
        self._blocked_responses: set[str] = set()
        self._blocked_commands: set[str] = set()
        self.has_spoken = False

    def session_update(self) -> dict:
        language = self.config.language
        plan = json.dumps({'goal': self._goal, 'summary_en': self.brief.summary_en,
                           'share': self.brief.share,
                           'never': self.brief.never}, ensure_ascii=False)
        session = {
            'type': 'realtime', 'model': self.config.realtime_model,
            'instructions': language.plan_label + plan + '\n' + language.instructions,
            'output_modalities': ['audio'],
            'audio': {
                'input': {'format': {'type': 'audio/pcmu'},
                          'transcription': {'model': self.config.transcribe_model,
                                            'language': self.config.call_language},
                          'noise_reduction': (None if self.config.vad_noise_reduction == 'off'
                                              else {'type': self.config.vad_noise_reduction}),
                          'turn_detection': self._turn_detection()},
                'output': {'format': {'type': 'audio/pcmu'},
                           'voice': self.config.realtime_voice,
                           'speed': self.config.realtime_speed},
            },
            'tools': list(self._tools),
        }
        if self.config.realtime_reasoning is not None:
            session['reasoning'] = {'effort': self.config.realtime_reasoning}
        return {'type': EVENTS['session_update'], 'session': session}

    def _turn_detection(self) -> dict:
        # Transcription is asynchronous to VAD: classify the final text before replying.
        return {'type': 'server_vad', 'threshold': self.config.vad_threshold,
                'silence_duration_ms': self.config.vad_silence_duration_ms,
                'create_response': False, 'interrupt_response': False}

    async def _send(self, key: str, **payload) -> None:
        if self._ws is None or self._stopped:
            raise RealtimeError('The voice connection is unavailable.')
        try:
            if key == 'response_cancel':
                self.timeline.record('response_cancel', **payload)
            await self._ws.send_json({'type': EVENTS[key], **payload})
        except Exception:
            raise RealtimeError('The voice connection ended.') from None

    async def connect(self) -> None:
        """Pre-warm without generating a response or sending any call content audio."""
        async with self._connect_lock:
            if self._stopped or not self.config.enabled:
                raise RealtimeError('Calling is disabled or the voice connection has ended.')
            if self._ws is not None:
                return
            try:
                if self._session is None:
                    self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(
                    REALTIME_URL, params={'model': self.config.realtime_model},
                    headers={'Authorization': 'Bearer ' + self.config.openai_api_key},
                    heartbeat=20, timeout=10, max_msg_size=4 * 1024 * 1024,
                )
                await self._ws.send_json(self.session_update())
                async with asyncio.timeout(10):
                    while True:
                        event = await self._receive()
                        if event['type'] == EVENTS['session_updated']:
                            break
                        if event['type'] == EVENTS['error']:
                            self._log_error(event)
                            raise RealtimeError('The voice session could not be configured.')
                        # rate_limits.updated and other unrelated setup events are harmless.
                self._task = asyncio.create_task(self._run())
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception:
                await self.stop()
                raise RealtimeError('The voice service could not be connected.') from None

    async def start(self, media: TwilioMedia) -> None:
        if self._media is not None or self._stopped:
            raise RealtimeError('The voice connection cannot be reused.')
        self._media = media
        self._answered_at = self._clock()
        await self.connect()

    async def open_conversation(self) -> None:
        """Called by the engine only after its hello/AMD gate opens."""
        if self._opener_requested or self._silenced or self._stopped or self._media is None:
            return
        self._opener_requested = True
        await self.repeat_notice()

    async def repeat_notice(self) -> None:
        """Renew the actual notice after transfer, before classifying the new reply."""
        if self._silenced or self._stopped or self._media is None:
            return
        self._conversation_open = False
        generated = await self.say(self.brief.opener_ja, _opener=True)
        if await generated != 'completed':
            raise RealtimeError('The voice service could not speak the opening.')
        if not self._silenced and not self._stopped:
            self._conversation_open = True
            self._schedule_reply()

    def set_permission(self, granted: bool) -> None:
        """Fence queued speech before the caller awaits any consent cleanup."""
        if self._stopped:
            return
        if granted:
            self._permission = True
            return
        self._permission = False
        self._permission_generation += 1
        self._blocked_commands.update(self._commands)
        self._blocked_responses.update(self._active_responses)
        self._blocked_responses.update(self._allowed_responses)
        self._allowed_responses.clear()
        self._marks.clear()
        self.audio_sink.clear('fri')
        if not self._stopped and self._media is not None:
            responses = tuple(self._active_responses)
            previous = self._revocation

            async def revoke():
                if previous is not None:
                    await asyncio.shield(previous)
                for response_id in responses:
                    await self._send('response_cancel', response_id=response_id)
                await self._barge_in()

            self._revocation = self._background(revoke())

    def silence(self) -> None:
        """Fence output synchronously before voicemail cleanup can await anything."""
        self._silenced = True
        if self.audio_sink is not None:
            self.audio_sink.clear('fri')
        self._marks.clear()
        self._conversation_open = False
        self._blocked_commands.update(self._commands)
        self._blocked_responses.update(self._active_responses)
        self._blocked_responses.update(self._allowed_responses)
        self._allowed_responses.clear()

    async def interrupt(self) -> None:
        self.silence()
        for response_id in tuple(self._active_responses):
            await self._send('response_cancel', response_id=response_id)
        await self._barge_in()

    def _output_allowed(self, event: dict) -> bool:
        response_id = event.get('response_id', '')
        return (not self._silenced and not self._stopped
                and response_id not in self._blocked_responses
                and response_id in self._allowed_responses)

    async def _submit(self, command: _Command) -> None:
        async with self._response_lock:
            await self._idle.wait()
            if self._revocation is not None:
                await asyncio.shield(self._revocation)
            if (command.generation != self._permission_generation
                    or command.number in self._blocked_commands):
                self._commands.pop(command.number, None)
                if not command.done.done():
                    command.done.set_result('cancelled')
                return
            if self._stopped:
                raise RealtimeError('The voice connection ended.')
            if not self._permission and not command.opener:
                command.response.update(output_modalities=['text'], tools=[
                    tool for tool in self._tools if tool.get('name') == 'permission'],
                    tool_choice={'type': 'function', 'name': 'permission'})
                command.response['instructions'] = (
                    'Use only permission(decision=granted/refused/transfer) to classify the '
                    'callee reply to the notice. Ambiguous input grants nothing. Do not speak '
                    'or start the inquiry.')
            self._idle.clear()
            self._pending = command
            await self._send('response_create', event_id='jpcall-' + command.number,
                             response=command.response)

    def _background(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._side_tasks.add(task)

        def finished(done):
            self._side_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Errors are handled by the owning call's connection monitor.
        task.add_done_callback(finished)
        return task

    async def respond(self, *, instructions: str | None = None,
                      out_of_band: bool = False, _opener: bool = False) -> asyncio.Future:
        self._response_seq += 1
        number = str(self._response_seq)
        response = {'metadata': {'jpcall_command': number}}
        if instructions is not None:
            response['instructions'] = instructions
        if out_of_band:
            response.update(conversation='none', input=[], tools=[], tool_choice='none')
        command = _Command(number, response, asyncio.get_running_loop().create_future(),
                           self._permission_generation,
                           self._publication_context() if self._publication_context else None,
                           _opener)
        self._commands[number] = command

        def cancelled(done):
            if done.cancelled() and not self._stopped:
                self._blocked_commands.add(number)
                responses = tuple(response_id for response_id, command_number
                                  in self._response_commands.items() if command_number == number)
                self._blocked_responses.update(responses)

                async def cancel():
                    for response_id in responses:
                        await self._send('response_cancel', response_id=response_id)
                    if responses:
                        await self._barge_in()

                self._background(cancel())
        command.done.add_done_callback(cancelled)
        try:
            await self._submit(command)
        except BaseException:
            self._commands.pop(number, None)
            if self._pending is command:
                self._pending = None
                self._update_idle()
            command.done.cancel()
            raise
        return command.done

    async def say(self, text_ja: str, *, out_of_band: bool = False,
                  _opener: bool = False) -> asyncio.Future:
        return await self.respond(instructions=(
            self.config.language.instructions + '\n' + self.config.language.say_prefix
            + json.dumps(text_ja, ensure_ascii=False)), out_of_band=out_of_band,
            _opener=_opener)

    async def drain(self, timeout_s: float = 8) -> None:
        async with asyncio.timeout(timeout_s):
            await self._drained.wait()

    def _update_idle(self) -> None:
        if not self._active_responses and self._pending is None and not self._busy_unknown:
            self._idle.set()
        if not self._active_responses and not self._marks and not self._generating:
            self._drained.set()

    @staticmethod
    def _log_error(event: dict) -> str:
        raw = (event.get('error') or {}).get('code', '')
        valid = isinstance(raw, str) and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', raw)
        code = raw if valid else 'unknown'
        LOG.warning('jpcall realtime error code=%s', code)
        return code

    async def _receive(self) -> dict:
        message = await self._ws.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RealtimeError('The voice connection ended.')
        event = json.loads(message.data)
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise RealtimeError('The voice service sent an invalid event.')
        return event

    async def _run(self) -> None:
        try:
            while not self._stopped:
                await self.handle_event(await self._receive())
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RealtimeError('The voice connection ended.') from None
        finally:
            await self._close_media()

    async def _close_media(self) -> None:
        if self._media is None or self._media_closed:
            return
        self._media_closed = True
        with suppress(Exception):
            await self._media.clear()
        if self.audio_sink is not None:
            self.audio_sink.clear('fri')
        if self._media is not None:
            with suppress(Exception):
                await self._media.close()

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def audio(self, ulaw: bytes) -> None:
        await self._send('input_append', audio=base64.b64encode(ulaw).decode())

    async def mark(self, name: str) -> None:
        # Clear acknowledgements arrive with the same mark names; removed marks
        # must never be interpreted as played audio after interruption.
        pending = self._marks.pop(name, None)
        if pending is not None:
            key, end = pending
            part = self._playout.get(key)
            if part is not None and part.item_id not in self._interrupted:
                part.played_bytes = max(part.played_bytes, end)
                recent = next((text for _, item, text in self._recent_speech
                               if item == part.item_id), self._transcripts.get(key))
                if recent is not None:
                    self._remember_speech(part.item_id, recent)
                await self._flush_transcript(key)
                self._confirm_response(part.response_id)
        self._update_idle()

    async def _barge_in(self) -> None:
        async with self._output_lock:
            # Invalidate marks BEFORE awaiting clear: Twilio echoes cleared marks.
            pending = [part for key, part in self._playout.items()
                       if (part.sent_bytes > part.played_bytes or key in self._generating)
                       and part.item_id not in self._interrupted]
            self._interrupted.update(part.item_id for part in pending)
            self._marks.clear()
            self._generating.clear()
            self.audio_sink.clear('fri')
            if self._media is not None:
                await self._media.clear()
            for part in pending:
                self.timeline.record('truncate', item_id=part.item_id,
                                     response_id=part.response_id,
                                     audio_end_ms=part.played_bytes // 8)
                await self._send('truncate', item_id=part.item_id,
                                 content_index=part.content_index,
                                 audio_end_ms=part.played_bytes // 8)
                await self._flush_transcript((part.item_id, part.content_index))
            self._update_idle()

    async def _utterance(self, item_id: str, speaker: str, text: str,
                         started_at: float, *, ended_at: float | None = None,
                         delivery: str = 'complete',
                         consent_generation: int | None = None) -> None:
        key = speaker, item_id
        if key in self._utterances:
            return
        self._utterances.add(key)
        if speaker == 'fri' and delivery == 'complete':
            self._remember_speech(item_id, text)
        self.timeline.record('transcript', speaker=speaker, item_id=item_id, text=text)
        await self.events.emit(Utterance(self.brief.request_id, item_id, speaker,
                                         text, started_at, ended_at, delivery,
                                         consent_generation=consent_generation))

    @staticmethod
    def _normalized(text: str) -> str:
        return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())

    def _is_echo(self, text: str) -> bool:
        now = self._clock()
        while self._recent_speech and now - self._recent_speech[0][0] > 10:
            self._recent_speech.popleft()
        normalized = self._normalized(text)
        # Common short answers (はい / 네 / いいえ) are not evidence of an echo.
        if len(normalized) < 6:
            return False
        if any(normalized == spoken for _, _, spoken in self._recent_speech):
            return True
        return False

    def _remember_speech(self, item_id: str, text: str) -> None:
        self._recent_speech = deque((entry for entry in self._recent_speech
                                    if entry[1] != item_id and self._clock() - entry[0] <= 10),
                                   maxlen=64)
        self._recent_speech.append((self._clock(), item_id, self._normalized(text)))

    def _schedule_reply(self) -> None:
        if (self._reply_needed and self._conversation_open and not self._turn_pending
                and not self._silenced and not self._stopped):
            self._turn_pending = True
            self._background(self._reply_to_callee())

    async def _reply_to_callee(self) -> None:
        try:
            await self._idle.wait()
            if not self._stopped and not self._silenced:
                self._reply_needed = False
                await self.respond()
        finally:
            self._turn_pending = False
            self._schedule_reply()

    def _confirm_response(self, response_id: str) -> None:
        if (not response_id or response_id not in self._completed_responses
                or response_id in self._delivered_responses
                or response_id in self._blocked_responses or self._silenced or self._stopped):
            return
        parts = [part for part in self._playout.values() if part.response_id == response_id]
        if not parts or any(part.generation != self._permission_generation
                            or not part.sent_bytes or part.played_bytes < part.sent_bytes
                            or part.item_id in self._interrupted
                            or (part.item_id, part.content_index) in self._generating
                            for part in parts):
            return
        self._delivered_responses.add(response_id)
        if response_id not in self._opener_responses and self._on_callee_response:
            self._on_callee_response()

    async def _asr_timeout(self, item: str) -> None:
        await asyncio.sleep(ASR_TIMEOUT_S)
        self._asr_timers.pop(item, None)
        if item not in self._input_seen and item not in self._asr_failed_items:
            self._asr_failed_items.add(item)
            if item not in self._preopening_items:
                self._asr_failed()

    def _asr_failed(self) -> None:
        if self._silenced or self._stopped or not self._opener_requested:
            return
        if self._asr_recovery is None or self._asr_recovery.done():
            self._asr_ok.clear()
            self._asr_recovery = self._background(self._recover_asr())

    async def _exhaust_asr(self) -> None:
        self.silence()
        await self._barge_in()
        if self._on_asr_failure:
            await self._on_asr_failure()
        else:
            await self._close_media()

    async def _recover_asr(self) -> None:
        if self._asr_clarified:
            await self._exhaust_asr()
            return
        self._asr_clarified = True
        if self._asr_ok.is_set():
            return
        try:
            async with asyncio.timeout(ASR_TIMEOUT_S):
                await self._idle.wait()
                if self._permission:
                    done = await self.respond(out_of_band=True, instructions=(
                        self.config.language.instructions + '\nIn '
                        + self.config.language.name
                        + ', briefly say you could not hear and ask the callee to repeat. '
                        'Do not state or confirm any inquiry facts.'))
                else:
                    done = await self.say(self.brief.opener_ja, _opener=True)
                if await done != 'completed':
                    raise RealtimeError('The voice service could not clarify.')
                await self.drain(timeout_s=ASR_TIMEOUT_S)
            async with asyncio.timeout(ASR_TIMEOUT_S):
                await self._asr_ok.wait()
        except (TimeoutError, RealtimeError):
            await self._exhaust_asr()

    async def _flush_transcript(self, key: tuple[str, int]) -> None:
        if key not in self._transcripts:
            return
        part = self._playout.get(key)
        interrupted = key[0] in self._interrupted
        if part is None or part.generation != self._permission_generation:
            self._transcripts.pop(key, None)
            return
        if not interrupted and (part.played_bytes < part.sent_bytes
                                or key in self._generating):
            return
        text = self._transcripts.pop(key)
        if not part.played_bytes:
            return
        await self._utterance(key[0], 'fri', text, part.started_at,
                              ended_at=self._clock() - self._answered_at,
                              delivery='interrupted' if interrupted else 'complete',
                              consent_generation=part.consent_generation)

    async def handle_event(self, event: dict) -> None:
        if self._stopped:
            return
        kind = event.get('type')
        if kind == EVENTS['response_created']:
            response = event['response']
            self.timeline.record('response_created', response_id=response['id'],
                                 status=response.get('status'))
            self._active_responses.add(response['id'])
            self._idle.clear()
            self._drained.clear()
            number = (response.get('metadata') or {}).get('jpcall_command')
            if self._pending and self._pending.number == number:
                self._pending = None
            command = self._commands.get(number)
            self._response_context.setdefault(response['id'],
                command.consent_generation if command is not None else None)
            if command is not None:
                self._response_commands[response['id']] = number
            explicit = (command is not None and command.opener
                        and command.generation == self._permission_generation
                        and number not in self._blocked_commands)
            if explicit:
                self._opener_responses.add(response['id'])
            if explicit or (self._permission and self._conversation_open):
                self._allowed_responses.add(response['id'])
            if (self._silenced or (not explicit and not self._conversation_open)
                    or number in self._blocked_commands):
                self._blocked_responses.add(response['id'])
                await self._send('response_cancel', response_id=response['id'])
        elif kind == EVENTS['speech_started']:
            self.timeline.record('speech_started', item_id=event['item_id'],
                                 audio_start_ms=event['audio_start_ms'])
            self._starts[event['item_id']] = max(0, event['audio_start_ms'] / 1000)
            self._input_context.setdefault(event['item_id'],
                self._publication_context() if self._publication_context else None)
            if not self._opener_requested:
                self._preopening_items.add(event['item_id'])
            if self._on_speech:
                self._on_speech(True, event['item_id'])
        elif kind == EVENTS['speech_stopped']:
            self.timeline.record('speech_stopped', item_id=event['item_id'],
                                 audio_end_ms=event.get('audio_end_ms'))
            item = event['item_id']
            self._ends[item] = max(0, event.get(
                'audio_end_ms', (self._clock() - self._answered_at) * 1000) / 1000)
            if item not in self._input_seen and item not in self._asr_timers:
                self._asr_timers[item] = self._background(self._asr_timeout(item))
            if self._on_speech:
                self._on_speech(False, item)
        elif kind == EVENTS['input_transcript_failed']:
            item = event['item_id']
            if item not in self._input_seen and item not in self._asr_failed_items:
                self._asr_failed_items.add(item)
                timer = self._asr_timers.pop(item, None)
                if timer is not None:
                    timer.cancel()
                if self._on_speech:
                    self._on_speech(False, item)
                if item not in self._preopening_items:
                    self._asr_failed()
        elif kind == EVENTS['input_transcript']:
            item = event['item_id']
            if item in self._input_seen:
                return
            self._input_seen.add(item)
            eligible = self._opener_requested and item not in self._preopening_items
            timer = self._asr_timers.pop(item, None)
            if timer is not None:
                timer.cancel()
            self._preopening_items.discard(item)
            self.meter.transcription(event.get('usage'), self.config.transcribe_model, item)
            text = event['transcript']
            if self._on_speech:
                self._on_speech(False, item)
            if not text.strip():
                if eligible:
                    self._asr_failed()
                return
            echo = self._is_echo(text)
            if echo:
                self.timeline.record('echo_uncertain', item_id=item, text=text)
            await self._utterance(item, 'callee', text,
                                  self._starts.get(item, self._clock() - self._answered_at),
                                  ended_at=self._ends.get(item),
                                  delivery='unknown' if echo else 'complete',
                                  consent_generation=self._input_context.get(item))
            if eligible and not self._silenced:
                if echo:
                    self._asr_failed()
                    return
                self._asr_ok.set()
                self._asr_clarified = False
                if self._asr_recovery is not None:
                    self._asr_recovery.cancel()
                    self._asr_recovery = None
                if self._conversation_open:
                    # Confirmed speech interrupts; raw VAD noise does not.
                    for response_id in tuple(self._active_responses):
                        self._blocked_responses.add(response_id)
                        await self._send('response_cancel', response_id=response_id)
                    await self._barge_in()
                self._reply_needed = True
                self._schedule_reply()
        elif kind == EVENTS['output_audio']:
            if not self._output_allowed(event):
                return
            item = event['item_id']
            if item in self._interrupted:
                return
            ulaw = base64.b64decode(event['delta'], validate=True)
            if not ulaw:
                return
            key = item, event['content_index']
            self._generating.add(key)
            self._drained.clear()
            async with self._output_lock:
                if item in self._interrupted or self._stopped or not self._output_allowed(event):
                    return
                part = self._playout.setdefault(key, _Playout(
                    item, key[1], started_at=self._clock() - self._answered_at,
                    response_id=event.get('response_id', ''),
                    generation=self._permission_generation,
                    consent_generation=self._response_context.get(event.get('response_id', ''))))
                self.has_spoken = True
                await self._media.send_audio(ulaw)
                part.sent_bytes += len(ulaw)
                if not self._output_allowed(event):
                    return
                self.audio_sink.push('fri', ulaw_to_pcm16(ulaw))
                self._mark_seq += 1
                name = str(self._mark_seq)
                self._marks[name] = key, part.sent_bytes
                await self._media.mark(name)
        elif kind == EVENTS['output_audio_done']:
            key = event['item_id'], event['content_index']
            self._generating.discard(key)
            await self._flush_transcript(key)
            part = self._playout.get(key)
            if part:
                self._confirm_response(part.response_id)
            self._update_idle()
        elif kind == EVENTS['output_transcript_delta']:
            if self._output_allowed(event):
                item = event['item_id']
                text = self._partial_transcripts.get(item, '') + event['delta']
                self._partial_transcripts[item] = text
                self._remember_speech(item, text)
        elif kind == EVENTS['output_transcript']:
            key = event['item_id'], event['content_index']
            if (not self._output_allowed(event) and not (
                    not self._silenced and key in self._playout and key[0] in self._interrupted)):
                return
            self._partial_transcripts.pop(key[0], None)
            self._remember_speech(key[0], event['transcript'])
            self._transcripts[key] = event['transcript']
            await self._flush_transcript(key)
        elif kind == EVENTS['response_done']:
            self.meter.realtime(event, self.config.realtime_model)
            response = event.get('response') or {}
            self.timeline.record('response_done', response_id=response.get('id'),
                                 status=response.get('status'), truncated=any(
                                     part.response_id == response.get('id')
                                     and part.item_id in self._interrupted
                                     for part in self._playout.values()))
            response_id = response.get('id', '')
            if response.get('status') == 'completed':
                self._completed_responses.add(response_id)
                self._confirm_response(response_id)
            self._active_responses.discard(response_id)
            self._busy_unknown = False
            number = (response.get('metadata') or {}).get('jpcall_command')
            command = self._commands.pop(number, None)
            if command:
                if self._pending is command:
                    self._pending = None
                if not command.done.done():
                    command.done.set_result(response.get('status'))
            self._update_idle()
            if (self._conversation_open and response.get('id') not in self._blocked_responses
                    and response.get('status') == 'completed' and self._on_tool_call is not None):
                for item in response.get('output', []):
                    if item.get('type') != 'function_call' or item.get('status') != 'completed':
                        continue
                    call_id = item['call_id']
                    if call_id in self._tools_seen:
                        continue
                    self._tools_seen.add(call_id)
                    self.timeline.record('tool_call', call_id=call_id, name=item['name'],
                                         arguments=item.get('arguments'))
                    try:
                        arguments = json.loads(item['arguments'])
                        if not isinstance(arguments, dict):
                            raise ValueError
                    except (ValueError, TypeError):
                        self._background(self.tool_result(call_id, {'error': 'Invalid arguments'}))
                        continue
                    if not self._permission and item['name'] != 'permission':
                        continue
                    await self._on_tool_call(item['name'], arguments, call_id)
        elif kind == EVENTS['error']:
            code = self._log_error(event)
            if (code in FATAL_CODES
                    or (event.get('error') or {}).get('type') == 'authentication_error'):
                raise RealtimeError('The voice session is unavailable.')
            command = self._pending
            error_id = (event.get('error') or {}).get('event_id')
            if command is not None and (error_id == 'jpcall-' + command.number
                                        or code in BUSY_CODES):
                self._pending = None
                if code in BUSY_CODES:
                    self._busy_unknown = True
                    self._idle.clear()
                    self._background(self._submit(command))
                else:
                    self._commands.pop(command.number, None)
                    if not command.done.done():
                        command.done.set_result(None)
            self._update_idle()

    async def tool_result(self, call_id: str, result: dict, *, respond: bool = True) -> None:
        self.timeline.record('tool_result', call_id=call_id, result=result)
        await self._send('item_create', item={'type': 'function_call_output',
                                             'call_id': call_id, 'output': json.dumps(result)})
        if respond:
            await self.respond()

    def force_cleanup(self) -> None:
        """Drop local content and cancel work even if a transport refuses to close."""
        self._stopped = True
        self._silenced = True
        self._permission = self._conversation_open = False
        self._idle.set()
        self._drained.set()
        for command in self._commands.values():
            command.done.cancel()
            command.response.clear()
        self._commands.clear()
        self._pending = None
        current = asyncio.current_task()
        for task in (*self._side_tasks, self._task):
            if task is not None and task is not current:
                task.cancel()
        self._side_tasks.clear()
        self._task = self._revocation = self._asr_recovery = None
        if self.audio_sink is not None:
            with suppress(Exception):
                self.audio_sink.clear('fri')
        self._media_closed = True
        # Abort the HTTP response before attempting the WebSocket closing handshake.
        response = getattr(self._ws, '_response', None)
        if response is not None:
            with suppress(Exception):
                response.close()
        self._ws = self._media = self._session = None
        self.config = self.brief = self.events = self.audio_sink = None
        self._on_speech = self._on_tool_call = None
        self._on_asr_failure = self._on_callee_response = self._publication_context = None
        self._goal, self._tools = '', ()
        self._clock = time.monotonic
        self._playout.clear()
        self._marks.clear()
        self._generating.clear()
        self._active_responses.clear()
        self._response_commands.clear()
        self._allowed_responses.clear()
        self._blocked_responses.clear()
        self._blocked_commands.clear()
        self._opener_responses.clear()
        self._completed_responses.clear()
        self._delivered_responses.clear()
        self._utterances.clear()
        self._tools_seen.clear()
        self._interrupted.clear()
        self._starts.clear()
        self._ends.clear()
        self._input_context.clear()
        self._response_context.clear()
        self._asr_timers.clear()
        self._asr_failed_items.clear()
        self._input_seen.clear()
        self._preopening_items.clear()
        self._transcripts.clear()
        self._recent_speech.clear()
        self._partial_transcripts.clear()
        self.timeline.close()

    @staticmethod
    async def _close_transport(transport, *, clear: bool = False) -> None:
        if clear:
            with suppress(Exception):
                async with asyncio.timeout(TRANSPORT_CLOSE_TIMEOUT_S):
                    await transport.clear()
        with suppress(Exception):
            async with asyncio.timeout(TRANSPORT_CLOSE_TIMEOUT_S):
                await transport.close()

    async def stop(self) -> None:
        tasks = tuple(task for task in (*self._side_tasks, self._task)
                      if task is not None and task is not asyncio.current_task())
        transports = [(transport, clear) for transport, clear in (
            (self._ws, False), (self._media, True),
            (self._session if self._owned else None, False)) if transport is not None]
        self.force_cleanup()
        try:
            async with asyncio.timeout(2 * TRANSPORT_CLOSE_TIMEOUT_S):
                await asyncio.gather(*tasks, *(self._close_transport(transport, clear=clear)
                                              for transport, clear in transports),
                                     return_exceptions=True)
        except TimeoutError:
            pass
        finally:
            self.force_cleanup()
