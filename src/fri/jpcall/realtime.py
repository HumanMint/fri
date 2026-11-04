"""GA Realtime WebSocket bridge. No member microphone audio enters this client.

Protocol references checked 2026-11-03:
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
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

import aiohttp

from .config import JpcallConfig
from .contract import AudioSink, CallBrief, EventSink, Utterance
from .server import TwilioMedia, ulaw_to_pcm16
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
    'output_audio': 'response.output_audio.delta',
    'output_audio_done': 'response.output_audio.done',
    'output_transcript': 'response.output_audio_transcript.done',
    'response_created': 'response.created',
    'response_done': 'response.done',
    'response_create': 'response.create',
    'response_cancel': 'response.cancel',
    'truncate': 'conversation.item.truncate',
    'item_create': 'conversation.item.create',
    'function_arguments_done': 'response.function_call_arguments.done',
    'error': 'error',
}
REALTIME_URL = 'wss://api.openai.com/v1/realtime'
LOG = logging.getLogger(__name__)
FATAL_CODES = frozenset({'invalid_api_key', 'authentication_error', 'permission_denied',
                         'session_expired', 'session_not_found', 'invalid_session',
                         'model_not_found'})
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


@dataclass
class _Command:
    number: str
    response: dict
    done: asyncio.Future = field(repr=False)


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
                 on_speech: Callable[[bool, str], None] | None = None):
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
        self._playout: dict[tuple[str, int], _Playout] = {}
        self._generating: set[tuple[str, int]] = set()
        self._marks: dict[str, tuple[tuple[str, int], int]] = {}
        self._mark_seq = 0
        self._interrupted: set[str] = set()
        self._starts: dict[str, float] = {}
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
        self._silenced = False
        self._allowed_responses: set[str] = set()
        self._blocked_responses: set[str] = set()
        self._blocked_commands: set[str] = set()
        self.has_spoken = False

    def session_update(self) -> dict:
        language = self.config.language
        plan = json.dumps({'summary': self.brief.summary_en, 'share': self.brief.share,
                           'never': self.brief.never}, ensure_ascii=False)
        return {'type': EVENTS['session_update'], 'session': {
            'type': 'realtime', 'model': self.config.realtime_model,
            'instructions': language.instructions + language.plan_label + plan,
            'output_modalities': ['audio'],
            'audio': {
                'input': {'format': {'type': 'audio/pcmu'},
                          'transcription': {'model': self.config.transcribe_model,
                                            'language': self.config.call_language},
                          'turn_detection': {'type': 'server_vad', 'create_response': False,
                                             'interrupt_response': False}},
                'output': {'format': {'type': 'audio/pcmu'},
                           'voice': self.config.realtime_voice,
                           'speed': self.config.realtime_speed},
            },
            'tools': list(self._tools),
        }}

    async def _send(self, key: str, **payload) -> None:
        if self._ws is None or self._stopped:
            raise RealtimeError('The voice connection is unavailable.')
        try:
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

    async def _auto_responses(self, enabled: bool) -> None:
        await self._send('session_update', session={'type': 'realtime', 'audio': {'input': {
            'turn_detection': {'type': 'server_vad', 'create_response': enabled,
                               'interrupt_response': enabled}}}})

    async def open_conversation(self) -> None:
        """Called by the engine only after its hello/AMD gate opens."""
        if self._opener_requested or self._silenced or self._stopped or self._media is None:
            return
        self._opener_requested = True
        generated = await self.say(self.brief.opener_ja)
        if await generated != 'completed':
            raise RealtimeError('The voice service could not speak the opening.')
        if not self._silenced and not self._stopped:
            self._conversation_open = True
            await self._auto_responses(True)

    def silence(self) -> None:
        """Fence output synchronously before voicemail cleanup can await anything."""
        self._silenced = True
        self._conversation_open = False
        self._blocked_commands.update(self._commands)
        self._blocked_responses.update(self._active_responses)
        self._allowed_responses.clear()

    async def interrupt(self) -> None:
        self.silence()
        await self._auto_responses(False)
        for response_id in tuple(self._active_responses):
            await self._send('response_cancel', response_id=response_id)
        await self._barge_in()

    def _output_allowed(self, event: dict) -> bool:
        response_id = event.get('response_id', '')
        return (response_id not in self._blocked_responses
                and (self._conversation_open or response_id in self._allowed_responses))

    async def _submit(self, command: _Command) -> None:
        async with self._response_lock:
            await self._idle.wait()
            if self._stopped:
                raise RealtimeError('The voice connection ended.')
            self._idle.clear()
            self._pending = command
            await self._send('response_create', event_id='jpcall-' + command.number,
                             response=command.response)

    def _background(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._side_tasks.add(task)

        def finished(done):
            self._side_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Errors are handled by the owning call's connection monitor.
        task.add_done_callback(finished)

    async def respond(self, *, instructions: str | None = None,
                      out_of_band: bool = False) -> asyncio.Future:
        self._response_seq += 1
        number = str(self._response_seq)
        response = {'metadata': {'jpcall_command': number}}
        if instructions is not None:
            response['instructions'] = instructions
        if out_of_band:
            response.update(conversation='none', input=[], tools=[], tool_choice='none')
        command = _Command(number, response, asyncio.get_running_loop().create_future())
        self._commands[number] = command
        try:
            await self._submit(command)
        except BaseException:
            self._commands.pop(number, None)
            command.done.cancel()
            raise
        return command.done

    async def say(self, text_ja: str, *, out_of_band: bool = False) -> asyncio.Future:
        return await self.respond(instructions=(
            self.config.language.say_prefix
            + json.dumps(text_ja, ensure_ascii=False)), out_of_band=out_of_band)

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
        self.audio_sink.clear('fri')
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
                await self._flush_transcript(key)
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
                await self._send('truncate', item_id=part.item_id,
                                 content_index=part.content_index,
                                 audio_end_ms=part.played_bytes // 8)
                await self._flush_transcript((part.item_id, part.content_index))
            self._update_idle()

    async def _utterance(self, item_id: str, speaker: str, text: str,
                         started_at: float) -> None:
        key = speaker, item_id
        if key in self._utterances:
            return
        self._utterances.add(key)
        await self.events.emit(Utterance(self.brief.request_id, item_id, speaker,
                                         text, started_at))

    async def _flush_transcript(self, key: tuple[str, int]) -> None:
        if key not in self._transcripts:
            return
        part = self._playout.get(key)
        interrupted = key[0] in self._interrupted
        if part and not interrupted and (part.played_bytes < part.sent_bytes
                                         or key in self._generating):
            return
        text = self._transcripts.pop(key) + (' …' if interrupted else '')
        await self._utterance(key[0], 'fri', text,
                              part.started_at if part else self._clock() - self._answered_at)

    async def handle_event(self, event: dict) -> None:
        kind = event.get('type')
        if kind == EVENTS['response_created']:
            response = event['response']
            self._active_responses.add(response['id'])
            self._idle.clear()
            self._drained.clear()
            number = (response.get('metadata') or {}).get('jpcall_command')
            if self._pending and self._pending.number == number:
                self._pending = None
            explicit = (self._opener_requested and number in self._commands
                        and number not in self._blocked_commands)
            if self._conversation_open or explicit:
                self._allowed_responses.add(response['id'])
            else:
                self._blocked_responses.add(response['id'])
                await self._send('response_cancel', response_id=response['id'])
        elif kind == EVENTS['speech_started']:
            self._starts[event['item_id']] = max(0, event['audio_start_ms'] / 1000)
            if self._on_speech:
                self._on_speech(True, event['item_id'])
            await self._barge_in()
        elif kind == EVENTS['speech_stopped']:
            if self._on_speech:
                self._on_speech(False, event['item_id'])
        elif kind == EVENTS['input_transcript']:
            item = event['item_id']
            if self._on_speech:
                self._on_speech(False, item)
            self.meter.transcription(event.get('usage'), self.config.transcribe_model, item)
            await self._utterance(item, 'callee', event['transcript'],
                                  self._starts.get(item, self._clock() - self._answered_at))
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
                    item, key[1], started_at=self._clock() - self._answered_at))
                self.has_spoken = True
                await self._media.send_audio(ulaw)
                if not self._output_allowed(event):
                    return
                self.audio_sink.push('fri', ulaw_to_pcm16(ulaw))
                part.sent_bytes += len(ulaw)
                self._mark_seq += 1
                name = str(self._mark_seq)
                self._marks[name] = key, part.sent_bytes
                await self._media.mark(name)
        elif kind == EVENTS['output_audio_done']:
            key = event['item_id'], event['content_index']
            self._generating.discard(key)
            await self._flush_transcript(key)
            self._update_idle()
        elif kind == EVENTS['output_transcript']:
            if not self._output_allowed(event):
                return
            key = event['item_id'], event['content_index']
            self._transcripts[key] = event['transcript']
            await self._flush_transcript(key)
        elif kind == EVENTS['response_done']:
            self.meter.realtime(event, self.config.realtime_model)
            response = event.get('response') or {}
            self._active_responses.discard(response.get('id', ''))
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
                    try:
                        arguments = json.loads(item['arguments'])
                        if not isinstance(arguments, dict):
                            raise ValueError
                    except (ValueError, TypeError):
                        self._background(self.tool_result(call_id, {'error': 'Invalid arguments'}))
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

    async def tool_result(self, call_id: str, result: dict) -> None:
        await self._send('item_create', item={'type': 'function_call_output',
                                             'call_id': call_id, 'output': json.dumps(result)})
        await self.respond()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._idle.set()
        for command in self._commands.values():
            command.done.cancel()
        self._commands.clear()
        for task in tuple(self._side_tasks):
            task.cancel()
        if self._side_tasks:
            await asyncio.gather(*self._side_tasks, return_exceptions=True)
        self._side_tasks.clear()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            if self._task is not asyncio.current_task():
                self._task.cancel()
                with suppress(asyncio.CancelledError, RealtimeError):
                    await self._task
        await self._close_media()
        for key in tuple(self._transcripts):
            self._interrupted.add(key[0])
            await self._flush_transcript(key)
        self._playout.clear()
        self._marks.clear()
        self._starts.clear()
        if self._owned and self._session is not None:
            await self._session.close()
