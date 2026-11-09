"""Authenticated Twilio ingress. All callbacks run on the owning asyncio loop.

Use ``listen`` to disable access logging (media/status URLs contain bearer tokens)
and request-body decompression.
The media token is consumed before the first await following authentication.
"""

from __future__ import annotations

import audioop
import base64
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .config import JpcallConfig
from .contract import AudioSink
from .twilio import validate_signature

TWILIO_EVENTS = {name: name for name in ('connected', 'start', 'media', 'mark', 'stop', 'clear')}


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    return audioop.ulaw2lin(ulaw, 2)


def pcm16_to_ulaw(pcm16: bytes) -> bytes:
    return audioop.lin2ulaw(pcm16, 2)


class MediaHandler(Protocol):
    async def start(self, media: TwilioMedia) -> None: ...
    async def audio(self, ulaw: bytes) -> None: ...
    async def mark(self, name: str) -> None: ...
    async def stop(self) -> None: ...


class TwilioMedia:
    def __init__(self, ws: web.WebSocketResponse, stream_sid: str):
        self._ws = ws
        self.stream_sid = stream_sid

    async def send_audio(self, ulaw: bytes) -> None:
        await self._ws.send_json({'event': TWILIO_EVENTS['media'], 'streamSid': self.stream_sid,
                                 'media': {'payload': base64.b64encode(ulaw).decode()}})

    async def mark(self, name: str) -> None:
        await self._ws.send_json({'event': TWILIO_EVENTS['mark'], 'streamSid': self.stream_sid,
                                 'mark': {'name': name}})

    async def clear(self) -> None:
        await self._ws.send_json({'event': TWILIO_EVENTS['clear'], 'streamSid': self.stream_sid})

    async def close(self) -> None:
        await self._ws.close()


@dataclass(repr=False)
class CallRoutes:
    media_token: str
    status_token: str


@dataclass(repr=False)
class _Call:
    audio: AudioSink
    handler: MediaHandler
    on_status: Callable[[dict[str, str]], Awaitable[None]]
    expires: float
    call_sid: str = ''
    ws: web.WebSocketResponse | None = None
    routes: CallRoutes = field(default_factory=lambda: CallRoutes('', ''))
    relay_allowed: Callable[[], bool] = field(default=lambda: True, repr=False)


class JpcallServer:
    def __init__(self, config: JpcallConfig, *, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._clock = clock
        self._media: dict[str, _Call] = {}
        self._status: dict[str, _Call] = {}
        self.recovery_status = None
        self.app = web.Application(client_max_size=16 * 1024)
        self.app.router.add_post('/jpcall/status/{token}', self._status_callback)
        self.app.router.add_get('/jpcall/media/{token}', self._media_socket)
        self.app.on_shutdown.append(self._shutdown)

    def register(self, audio: AudioSink, handler: MediaHandler,
                 on_status: Callable[[dict[str, str]], Awaitable[None]],
                 *, ttl_s: float = 60, relay_allowed: Callable[[], bool] = lambda: True) -> CallRoutes:
        if ttl_s <= 0:
            raise ValueError('Call routing lifetime must be positive')
        self._prune()
        routes = CallRoutes(secrets.token_urlsafe(32), secrets.token_urlsafe(32))
        call = _Call(audio, handler, on_status, self._clock() + ttl_s, routes=routes,
                     relay_allowed=relay_allowed)
        self._media[routes.media_token] = call
        self._status[routes.status_token] = call
        return routes

    def bind(self, routes: CallRoutes, call_sid: str) -> None:
        """Reconcile the REST result with any earlier authenticated callback/start."""
        call = self._status[routes.status_token]
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}', call_sid):
            raise ValueError('Invalid call identifier')
        if call.call_sid and call.call_sid != call_sid:
            raise ValueError('Call route already bound')
        call.call_sid = call_sid

    async def revoke(self, routes: CallRoutes) -> None:
        call = self._status.pop(routes.status_token, None)
        self._media.pop(routes.media_token, None)
        if call and call.ws is not None:
            request = getattr(call.ws, '_req', None)
            transport = getattr(request, 'transport', None)
            if transport is not None:
                transport.close()
            await call.ws.close()

    def _prune(self) -> None:
        now = self._clock()
        for token, call in list(self._media.items()):
            if call.expires <= now:
                self._media.pop(token, None)
        for token, call in list(self._status.items()):
            if call.expires <= now and call.ws is None:
                self._status.pop(token, None)

    def _authenticated(self, request: web.Request, params=(), *, media: bool = False) -> bool:
        # Never derive a signed public URL from untrusted Host/Forwarded headers.
        base = self.config.public_base_url
        urls = {base + request.raw_path}
        if media:
            urls.add(base.replace('https://', 'wss://', 1) + request.raw_path)
            # Twilio documents a trailing-slash variant for WSS handshakes.
            urls.update(url + '/' for url in tuple(urls) if not url.endswith('/'))
        else:
            # Voice HTTPS callbacks omit the port when Twilio signs them.
            origin = urlsplit(base)
            hostname = origin.hostname or ''
            if ':' in hostname:
                hostname = '[' + hostname + ']'
            urls.add('https://' + hostname + request.raw_path)
        params = tuple(params)
        return any(validate_signature(self.config.twilio_auth_token, url, params,
                                      request.headers.get('X-Twilio-Signature', ''))
                   for url in urls)

    async def _status_callback(self, request: web.Request) -> web.Response:
        if request.query_string or request.content_type != 'application/x-www-form-urlencoded':
            raise web.HTTPForbidden()
        if 'Content-Encoding' in request.headers:
            # Twilio sends plain forms; encoded bodies are refused before any parsing.
            raise web.HTTPUnsupportedMediaType()
        try:
            params = await request.post()
        except (ValueError, LookupError):  # Undecodable bytes or an unknown charset.
            raise web.HTTPBadRequest() from None
        if not self._authenticated(request, params.items()):
            raise web.HTTPForbidden()
        if any(len(params.getall(key)) != 1 for key in params):
            raise web.HTTPBadRequest()
        if (params.get('AccountSid') != self.config.twilio_account_sid
                or not re.fullmatch(r'CA[0-9a-fA-F]{32}', params.get('CallSid', ''))):
            raise web.HTTPForbidden()
        safe = {key: params[key] for key in ('CallSid', 'CallStatus', 'CallDuration',
                                            'SequenceNumber', 'AnsweredBy') if key in params}
        self._prune()
        call = self._status.get(request.match_info['token'])
        if call is None or call.expires <= self._clock():
            if self.recovery_status is not None:
                try:
                    if self.recovery_status(request.match_info['token'], safe):
                        return web.Response(status=204)
                except Exception:
                    raise web.HTTPServiceUnavailable() from None
            raise web.HTTPNotFound()
        if call.call_sid and params.get('CallSid') != call.call_sid:
            raise web.HTTPForbidden()
        self.bind(call.routes, params['CallSid'])
        # A whitelist avoids passing member data/phone numbers into status consumers.
        try:
            await call.on_status(safe)
        except Exception:
            raise web.HTTPServiceUnavailable(reason='Call status could not be processed') from None
        return web.Response(status=204)

    async def _media_socket(self, request: web.Request) -> web.WebSocketResponse:
        if request.query_string or not self._authenticated(request, media=True):
            raise web.HTTPForbidden()
        self._prune()
        call = self._media.get(request.match_info['token'])
        if call is None:
            raise web.HTTPNotFound()
        self._media.pop(request.match_info['token'])
        # Status callbacks remain valid for the bounded call and billing grace period.
        call.expires = self._clock() + self.config.limits.max_call_s + 120
        ws = web.WebSocketResponse(max_msg_size=64 * 1024, heartbeat=30)
        call.ws = ws
        started = False
        try:
            await ws.prepare(request)
            stream_sid = ''
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                event = json.loads(message.data)
                kind = event['event']
                if kind == TWILIO_EVENTS['connected']:
                    if (started or event.get('protocol') != 'Call'
                            or event.get('version') != '1.0.0'):
                        raise ValueError
                elif kind == TWILIO_EVENTS['start']:
                    start = event['start']
                    if (started or start.get('accountSid') != self.config.twilio_account_sid
                            or not re.fullmatch(r'CA[0-9a-fA-F]{32}', start.get('callSid', ''))
                            or (call.call_sid and start.get('callSid') != call.call_sid)
                            or start.get('mediaFormat') != {'encoding': 'audio/x-mulaw',
                                                           'sampleRate': 8000, 'channels': 1}
                            or start.get('tracks') != ['inbound']):
                        raise ValueError
                    self.bind(call.routes, start['callSid'])
                    stream_sid = start['streamSid']
                    if not stream_sid or event.get('streamSid') != stream_sid:
                        raise ValueError
                    started = True
                    await call.handler.start(TwilioMedia(ws, stream_sid))
                elif not started or event.get('streamSid') != stream_sid:
                    raise ValueError
                elif kind == TWILIO_EVENTS['media']:
                    if event['media'].get('track') != 'inbound':
                        raise ValueError
                    ulaw = base64.b64decode(event['media']['payload'], validate=True)
                    if call.relay_allowed():
                        call.audio.push('callee', ulaw_to_pcm16(ulaw))
                    await call.handler.audio(ulaw)
                elif kind == TWILIO_EVENTS['mark']:
                    await call.handler.mark(event['mark']['name'])
                elif kind == TWILIO_EVENTS['stop']:
                    break
                # DTMF is not sent to the model. Unknown extension events are ignored.
        except Exception:
            # Never log a token, provider body, member content or a WebSocket frame.
            await ws.close(code=1008, message=b'Call media ended')
        finally:
            try:
                if started:
                    with suppress(Exception):
                        await call.handler.stop()
            finally:
                call.ws = None
                await ws.close()
        return ws

    async def _shutdown(self, app: web.Application) -> None:
        for call in list(self._status.values()):
            if call.ws is not None:
                await call.ws.close()
        self._media.clear()
        self._status.clear()

    async def listen(self) -> web.AppRunner:
        if not self.config.enabled:
            raise RuntimeError('Calling is disabled until its configuration is complete.')
        # Never inflate request bodies: decompression would run before signature checks.
        runner = web.AppRunner(self.app, access_log=None, auto_decompress=False)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.config.listen_host, self.config.listen_port).start()
        except BaseException:
            await runner.cleanup()
            raise
        return runner
