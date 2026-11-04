"""In-memory WebSocket peers and aiohttp requests: no sockets or API credentials."""

import asyncio
import base64
import hashlib
import hmac
import json
import socket
from types import SimpleNamespace
from urllib.parse import urlencode

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from multidict import MultiDict
from test_engine_config import environment
from test_engine_usage import realtime_usage

from fri.jpcall.config import JpcallConfig
from fri.jpcall.contract import CallBrief
from fri.jpcall.realtime import EVENTS, OPENING_NOTICES, RealtimeClient, RealtimeError
from fri.jpcall.server import JpcallServer, pcm16_to_ulaw, ulaw_to_pcm16
from fri.jpcall.usage import UsageMeter


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network connections are forbidden in engine tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


SID = 'CA' + '3' * 32
STREAM = 'MZ' + '4' * 32
ULAW = b'\xff' * 160  # 20 ms PCMU silence


class Audio:
    def __init__(self):
        self.frames = []
        self.cleared = []
        self.loop = None

    def push(self, speaker, pcm):
        self.loop = asyncio.get_running_loop()
        self.frames.append((speaker, pcm))

    def clear(self, speaker):
        self.cleared.append(speaker)

    def close(self):
        raise AssertionError('Session lifecycle owns close, not the media transport')


class Events:
    def __init__(self):
        self.items = []

    async def emit(self, event):
        self.items.append(event)


class RealtimePeer:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.feed({'type': EVENTS['session_created']})

    def feed(self, event):
        self.incoming.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                                 data=json.dumps(event)))

    async def send_json(self, event):
        self.sent.append(event)
        if event['type'] == EVENTS['session_update']:
            self.feed({'type': EVENTS['session_updated']})

    async def receive(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


class RealtimeHTTP:
    def __init__(self, peer):
        self.peer = peer
        self.connects = []

    async def ws_connect(self, url, **kwargs):
        self.connects.append((url, kwargs))
        return self.peer

    async def close(self):
        raise AssertionError('An injected client must not be closed')


class Media:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_audio(self, data):
        self.sent.append(('media', data))

    async def mark(self, name):
        self.sent.append(('mark', name))

    async def clear(self):
        self.sent.append(('clear',))

    async def close(self):
        self.closed = True


def bridge(**kwargs):
    config = JpcallConfig.from_env(environment())
    audio, events, meter, peer = Audio(), Events(), UsageMeter(), RealtimePeer()
    client = RealtimeClient(config, CallBrief('request1', 'Ask about lunch.', 'こんにちは。', (), ()),
                            audio, events, meter, session=RealtimeHTTP(peer), **kwargs)
    return client, audio, events, meter, peer


def test_realtime_ga_session_input_output_and_final_transcripts():
    async def run():
        client, audio, events, meter, peer = bridge()
        media = Media()
        await client.start(media)
        try:
            session = peer.sent[0]['session']
            assert session['type'] == 'realtime' and session['model'] == 'gpt-realtime-2.1'
            assert session['audio']['input']['format'] == {'type': 'audio/pcmu'}
            assert session['audio']['output']['format'] == {'type': 'audio/pcmu'}
            assert session['audio']['input']['transcription']['language'] == 'ja'
            assert session['audio']['output']['voice'] == 'marin'
            assert OPENING_NOTICES in session['instructions']
            assert OPENING_NOTICES in peer.sent[1]['response']['instructions']
            await client.audio(ULAW)
            assert peer.sent[-1] == {'type': EVENTS['input_append'],
                                     'audio': base64.b64encode(ULAW).decode()}
            for item, start in [('callee1', 1200), ('callee2', 2400)]:
                await client.handle_event({'type': EVENTS['speech_started'],
                                           'item_id': item, 'audio_start_ms': start})
            # Final transcriptions can complete out of order and can be replayed.
            for item in ('callee2', 'callee1', 'callee1'):
                await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': item,
                                           'transcript': 'はい。', 'usage': {
                                               'input_tokens': 10, 'output_tokens': 2,
                                               'input_token_details': {'audio_tokens': 10}}})
            assert [(e.item_id, e.started_at) for e in events.items] == [('callee2', 2.4),
                                                                       ('callee1', 1.2)]
            await client.handle_event({'type': EVENTS['output_audio'], 'item_id': 'fri1',
                                       'content_index': 0,
                                       'delta': base64.b64encode(ULAW).decode()})
            assert audio.frames == [('fri', ulaw_to_pcm16(ULAW))]
            assert audio.loop is asyncio.get_running_loop()
            assert media.sent[-2:] == [('media', ULAW), ('mark', '1')]
            transcript = {'type': EVENTS['output_transcript'], 'item_id': 'fri1',
                          'content_index': 0, 'transcript': 'ありがとうございます。'}
            await client.handle_event(transcript)
            await client.handle_event(transcript)
            assert len(events.items) == 3 and events.items[-1].speaker == 'fri'
            event = {'type': EVENTS['response_done'], 'response': {
                'id': 'r1', 'usage': realtime_usage(), 'status': 'completed', 'output': []}}
            await client.handle_event(event)
            await client.handle_event(event)
            assert len(meter.lines()) == 2
        finally:
            await client.stop()
        assert peer.closed and media.closed
    asyncio.run(run())


def test_barge_in_truncates_only_acknowledged_audio_and_ignores_clear_acks():
    async def run():
        client, audio, events, meter, peer = bridge()
        media = Media()
        await client.start(media)
        try:
            delta = {'type': EVENTS['output_audio'], 'item_id': 'fri1', 'content_index': 0,
                     'delta': base64.b64encode(ULAW).decode()}
            await client.handle_event(delta)
            await client.mark('1')  # first 20 ms actually played
            await client.handle_event(delta)  # next 20 ms still queued
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'callee1',
                                       'audio_start_ms': 100})
            assert peer.sent[-1] == {'type': EVENTS['truncate'], 'item_id': 'fri1',
                                     'content_index': 0, 'audio_end_ms': 20}
            assert audio.cleared == ['fri'] and media.sent[-1] == ('clear',)
            await client.mark('2')  # Twilio returns marks for cleared audio, too.
            count = len(media.sent)
            await client.handle_event(delta)  # late cancelled response chunk
            assert len(media.sent) == count
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'callee2',
                                       'audio_start_ms': 200})
            truncations = [e for e in peer.sent if e['type'] == EVENTS['truncate']]
            assert len(truncations) == 1
        finally:
            await client.stop()
    asyncio.run(run())


def test_tools_wait_for_completed_response_and_run_once():
    async def run():
        calls = []

        async def tool(name, arguments, call_id):
            calls.append((name, arguments, call_id))

        client, audio, events, meter, peer = bridge(on_tool_call=tool)
        media = Media()
        await client.start(media)
        try:
            item = {'type': 'function_call', 'name': 'ask_group', 'call_id': 'tool1',
                    'status': 'completed', 'arguments': '{"question":"Which time?"}'}
            await client.handle_event({'type': EVENTS['function_arguments_done'], **item})
            assert not calls
            event = {'type': EVENTS['response_done'], 'response': {'id': 'r1', 'usage': None,
                      'status': 'cancelled', 'output': [item]}}
            await client.handle_event(event)
            assert not calls
            event['response']['status'] = 'completed'
            await client.handle_event(event)
            await client.handle_event(event)
            assert calls == [('ask_group', {'question': 'Which time?'}, 'tool1')]
            await client.tool_result('tool1', {'answer': 'noon'})
            assert peer.sent[-2]['item']['type'] == 'function_call_output'
            assert peer.sent[-1]['type'] == EVENTS['response_create']
        finally:
            await client.stop()
    asyncio.run(run())


def test_receive_failure_closes_media_and_exposes_only_safe_error():
    async def run():
        client, audio, events, meter, peer = bridge()
        media = Media()
        await client.start(media)
        peer.feed({'type': EVENTS['error'], 'error': {'message': 'private-dialogue-secret'}})
        with pytest.raises(RealtimeError) as error:
            await asyncio.wait_for(client.wait(), 1)
        assert 'private' not in str(error.value)
        assert media.closed and 'fri' in audio.cleared
        await client.stop()
    asyncio.run(run())


class TwilioSocket:
    def __init__(self, frames):
        self.frames = frames
        self.sent = []
        self.closed = False

    async def prepare(self, request):
        pass

    def __aiter__(self):
        async def receive():
            for frame in self.frames:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame))
        return receive()

    async def send_json(self, event):
        self.sent.append(event)

    async def close(self, **kwargs):
        self.closed = True


class Handler:
    def __init__(self):
        self.frames = []
        self.marks = []
        self.stopped = False

    async def start(self, media):
        await media.send_audio(ULAW)
        await media.mark('out1')
        await media.clear()

    async def audio(self, ulaw):
        self.frames.append(ulaw)

    async def mark(self, name):
        self.marks.append(name)

    async def stop(self):
        self.stopped = True


def signed_request(config, path, token, params=None, signature=True, *, signed_url=None):
    body = '' if params is None else urlencode(params)
    payload = signed_url or config.public_base_url + path
    if params is not None:
        grouped = {}
        for k, v in params:
            grouped.setdefault(k, set()).add(v)
        payload += ''.join(k + v for k in sorted(grouped) for v in sorted(grouped[k]))
    digest = base64.b64encode(hmac.new(config.twilio_auth_token.encode(), payload.encode(),
                                      hashlib.sha1).digest()).decode()
    headers = {'X-Twilio-Signature': digest if signature else 'invalid',
               'Content-Type': 'application/x-www-form-urlencoded'}
    request = make_mocked_request('GET' if params is None else 'POST', path,
                                  headers=headers, match_info={'token': token})
    request._read_bytes = body.encode()
    if params is not None:
        request._post = MultiDict(params)
    return request


def setup_server(clock=lambda: 1):
    config = JpcallConfig.from_env(environment())
    server = JpcallServer(config, clock=clock)
    audio, handler, statuses = Audio(), Handler(), []

    async def status(params):
        statuses.append(params)

    routes = server.register(audio, handler, status)
    return config, server, audio, handler, statuses, routes


def start_frame(config, call_sid=SID):
    return {'event': 'start', 'streamSid': STREAM, 'start': {
        'accountSid': config.twilio_account_sid, 'callSid': call_sid, 'streamSid': STREAM,
        'tracks': ['inbound'], 'mediaFormat': {'encoding': 'audio/x-mulaw',
                                             'sampleRate': 8000, 'channels': 1}}}


def test_media_server_decodes_audio_and_consumes_token_once(monkeypatch):
    async def run():
        config, server, audio, handler, statuses, routes = setup_server()
        frames = [{'event': 'connected', 'protocol': 'Call', 'version': '1.0.0'},
                  start_frame(config), {'event': 'media', 'streamSid': STREAM,
                  'media': {'track': 'inbound', 'payload': base64.b64encode(ULAW).decode()}},
                  {'event': 'mark', 'streamSid': STREAM, 'mark': {'name': 'out1'}},
                  {'event': 'stop', 'streamSid': STREAM}]
        socket = TwilioSocket(frames)
        monkeypatch.setattr(web, 'WebSocketResponse', lambda **kwargs: socket)
        path = '/jpcall/media/' + routes.media_token
        # An authenticated start can race ahead of the create-call REST response.
        await server._media_socket(signed_request(config, path, routes.media_token))
        server.bind(routes, SID)
        assert audio.frames == [('callee', ulaw_to_pcm16(ULAW))]
        assert handler.frames == [ULAW] and handler.marks == ['out1'] and handler.stopped
        assert [event['event'] for event in socket.sent] == ['media', 'mark', 'clear']
        assert socket.sent[0]['streamSid'] == STREAM and socket.closed
        with pytest.raises(web.HTTPNotFound):
            await server._media_socket(signed_request(config, path, routes.media_token))
        assert routes.media_token not in repr(routes)
    asyncio.run(run())


def test_status_signature_account_call_and_expiry():
    async def run():
        now = [1.0]
        config, server, audio, handler, statuses, routes = setup_server(lambda: now[0])
        path = '/jpcall/status/' + routes.status_token
        params = [('AccountSid', config.twilio_account_sid), ('CallSid', SID),
                  ('CallStatus', 'completed'), ('To', '+81312345678')]
        with pytest.raises(web.HTTPForbidden):
            await server._status_callback(signed_request(config, path, routes.status_token,
                                                         params, signature=False))
        assert not statuses
        result = await server._status_callback(signed_request(config, path, routes.status_token,
                                                              params))
        assert result.status == 204 and statuses == [{'CallSid': SID, 'CallStatus': 'completed'}]
        with pytest.raises(web.HTTPForbidden):
            await server._status_callback(signed_request(config, path, routes.status_token,
                [(k, 'CA' + '9' * 32 if k == 'CallSid' else v) for k, v in params]))
        with pytest.raises(web.HTTPBadRequest):
            await server._status_callback(signed_request(config, path, routes.status_token,
                                                         params + [('CallStatus', 'ringing')]))
        now[0] = 100
        with pytest.raises(web.HTTPNotFound):
            await server._status_callback(signed_request(config, path, routes.status_token, params))
    asyncio.run(run())


def test_unknown_invalid_expired_and_revoked_media_tokens_are_rejected():
    async def run():
        now = [1.0]
        config, server, audio, handler, statuses, routes = setup_server(lambda: now[0])
        for token, valid_signature, error in [('unknown', True, web.HTTPNotFound),
                (routes.media_token, False, web.HTTPForbidden)]:
            with pytest.raises(error):
                await server._media_socket(signed_request(config, '/jpcall/media/' + token,
                                                          token, signature=valid_signature))
        now[0] = 100
        with pytest.raises(web.HTTPNotFound):
            await server._media_socket(signed_request(config, '/jpcall/media/' + routes.media_token,
                                                      routes.media_token))
        routes2 = server.register(audio, handler, lambda p: None)
        await server.revoke(routes2)
        with pytest.raises(web.HTTPNotFound):
            await server._media_socket(signed_request(
                config, '/jpcall/media/' + routes2.media_token, routes2.media_token))
    asyncio.run(run())


@pytest.mark.parametrize('bad_frame', [
    {'event': 'media', 'streamSid': STREAM, 'media': {'track': 'inbound', 'payload': '!!'}},
    {'event': 'media', 'streamSid': 'wrong', 'media': {'track': 'inbound', 'payload': '//8='}},
    {'event': 'media', 'streamSid': STREAM, 'media': {'track': 'outbound', 'payload': '//8='}},
])
def test_bad_media_closes_without_forwarding(monkeypatch, bad_frame):
    async def run():
        config, server, audio, handler, statuses, routes = setup_server()
        socket = TwilioSocket([start_frame(config), bad_frame])
        monkeypatch.setattr(web, 'WebSocketResponse', lambda **kwargs: socket)
        await server._media_socket(signed_request(config, '/jpcall/media/' + routes.media_token,
                                                  routes.media_token))
        assert not audio.frames and not handler.frames and socket.closed and handler.stopped
    asyncio.run(run())


def test_pcm_conversion_is_mono_8khz_and_little_endian():
    # Audioop preserves width; known ulaw extrema verify endian and signedness.
    assert ulaw_to_pcm16(b'\xff\x00\x80') == b'\x00\x00\x84\x82\x7c\x7d'
    assert pcm16_to_ulaw(b'\x00\x00' * 160) == ULAW


def test_websocket_signature_accepts_configured_wss_and_documented_slash(monkeypatch):
    async def run():
        config, server, audio, handler, statuses, routes = setup_server()
        path = '/jpcall/media/' + routes.media_token
        socket = TwilioSocket([start_frame(config)])
        monkeypatch.setattr(web, 'WebSocketResponse', lambda **kwargs: socket)
        signed_url = 'wss://calls.example.test' + path + '/'
        await server._media_socket(signed_request(config, path, routes.media_token,
                                                  signed_url=signed_url))
        assert handler.stopped and socket.closed
    asyncio.run(run())


def test_handshake_failure_closes_realtime_and_does_not_generate_opener():
    async def run():
        client, audio, events, meter, peer = bridge()
        peer.incoming = asyncio.Queue()
        peer.feed({'type': EVENTS['error'], 'error': {'message': 'private-provider-error'}})
        with pytest.raises(RealtimeError) as error:
            await client.start(Media())
        assert 'private' not in str(error.value) and peer.closed
        assert not any(e['type'] == EVENTS['response_create'] for e in peer.sent)
    asyncio.run(run())


def test_barge_in_blocks_late_chunks_when_all_prior_chunks_were_played():
    async def run():
        client, audio, events, meter, peer = bridge()
        media = Media()
        await client.start(media)
        try:
            delta = {'type': EVENTS['output_audio'], 'item_id': 'fri1', 'content_index': 0,
                     'delta': base64.b64encode(ULAW).decode()}
            await client.handle_event(delta)
            await client.mark('1')
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'callee1',
                                       'audio_start_ms': 100})
            assert peer.sent[-1]['audio_end_ms'] == 20
            count = len(audio.frames)
            await client.handle_event(delta)
            assert len(audio.frames) == count
        finally:
            await client.stop()
    asyncio.run(run())


def test_cancelled_handshake_closes_transport_and_propagates_cancellation():
    async def run():
        client, audio, events, meter, peer = bridge()
        ready = asyncio.Event()

        async def no_session_ack(event):
            peer.sent.append(event)
            ready.set()

        peer.send_json = no_session_ack
        media = Media()
        task = asyncio.create_task(client.start(media))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert peer.closed and media.closed
    asyncio.run(run())


def test_full_twilio_realtime_bridge_with_in_memory_peers(monkeypatch):
    async def run():
        client, audio, events, meter, peer = bridge()
        received_transcript = asyncio.Event()
        original_emit = events.emit
        original_send = peer.send_json

        async def emit(event):
            await original_emit(event)
            received_transcript.set()

        async def model(event):
            await original_send(event)
            if event['type'] == EVENTS['input_append']:
                peer.feed({'type': EVENTS['output_audio'], 'item_id': 'fri1', 'content_index': 0,
                           'delta': base64.b64encode(ULAW).decode()})
                peer.feed({'type': EVENTS['output_transcript'], 'item_id': 'fri1',
                           'content_index': 0, 'transcript': 'こんにちは。'})
                peer.feed({'type': EVENTS['response_done'], 'response': {
                    'id': 'r1', 'usage': realtime_usage(), 'status': 'completed', 'output': []}})

        peer.send_json, events.emit = model, emit
        config = JpcallConfig.from_env(environment())
        server = JpcallServer(config)

        async def status(params):
            pass

        routes = server.register(audio, client, status)
        server.bind(routes, SID)

        class ScriptedTwilio(TwilioSocket):
            def __aiter__(self):
                async def receive():
                    for event in [start_frame(config), {'event': 'media', 'streamSid': STREAM,
                            'media': {'track': 'inbound',
                                      'payload': base64.b64encode(ULAW).decode()}}]:
                        yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))
                    await asyncio.wait_for(received_transcript.wait(), 1)
                    name = next(e['mark']['name'] for e in self.sent if e['event'] == 'mark')
                    for event in [{'event': 'mark', 'streamSid': STREAM, 'mark': {'name': name}},
                                  {'event': 'stop', 'streamSid': STREAM}]:
                        yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))
                return receive()

        socket = ScriptedTwilio([])
        monkeypatch.setattr(web, 'WebSocketResponse', lambda **kwargs: socket)
        await server._media_socket(signed_request(config, '/jpcall/media/' + routes.media_token,
                                                  routes.media_token))
        assert audio.frames == [('callee', ulaw_to_pcm16(ULAW)), ('fri', ulaw_to_pcm16(ULAW))]
        assert events.items[0].text_ja == 'こんにちは。' and socket.closed and peer.closed
        assert meter.lines()[0].component == 'realtime'
    asyncio.run(run())
