"""Synthetic provider checks; this module never loads real runtime configuration."""

import ast
import asyncio
import importlib
import io
import json
import socket
import stat
import wave
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from fri.jpcall.config import CALL_LANGUAGES, JpcallConfig
from fri.jpcall.contract import Limits
from fri.jpcall.twilio import TwilioClient, TwilioError
from scripts import jpcall_owner_recording as recording

ACCOUNT = 'AC' + '1' * 32
CALL = 'CA' + '2' * 32
RECORDING = 'RE' + '3' * 32
DESTINATION = '+12025550123'  # Fictional fixture only.


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


class OwnerConfig(JpcallConfig):
    @property
    def language(self):
        return recording.owner_recording_language(context=context())


def config():
    return OwnerConfig(call_language='ko', twilio_account_sid=ACCOUNT,
                       twilio_api_key_sid='SK' + '4' * 32,
                       twilio_api_key_secret='synthetic-secret',
                       limits=Limits(max_call_s=120, test_numbers=frozenset({DESTINATION})))


def context():
    return recording.OwnerKoreanTestContext(DESTINATION, CALL, owner_only=True,
                                            explicit_go=True, recording_consent=True)


def wav(channels=2, frames=8000, *, silent=False):
    body = io.BytesIO()
    with wave.open(body, 'wb') as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        # High-byte-only activity verifies both bytes of PCM16, not just the low byte.
        audio.writeframes((b'\x00\x00' if silent else b'\x00\x01') * channels * frames)
    return body.getvalue()


class Provider:
    def __init__(self):
        self.requests = []
        self.call = dict(sid=CALL, to=DESTINATION, account_sid=ACCOUNT,
                         status='in-progress', direction='outbound-api')
        self.row = dict(sid=RECORDING, call_sid=CALL, account_sid=ACCOUNT,
                        status='completed', source='StartCallRecordingAPI', channels=2, duration='1')
        self.prior = []
        self.media = wav()
        self.media_status = 200
        self.start_status = 201

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.scheme == 'https' and request.url.host == 'api.twilio.com'
        assert not request.url.userinfo
        assert request.headers.get('Authorization', '').startswith('Basic ')
        prefix = '/2010-04-01/Accounts/' + ACCOUNT
        path = request.url.path.removeprefix(prefix)
        assert path != '/Calls.json'  # No endpoint in this helper creates telephone Calls.
        if path == '/Calls/' + CALL + '.json' and request.method == 'GET':
            return httpx.Response(200, json=self.call)
        if path == '/Calls/' + CALL + '/Recordings.json':
            if request.method == 'GET':
                return httpx.Response(200, json={'recordings': self.prior})
            assert request.method == 'POST'
            return httpx.Response(self.start_status, json=self.row)
        if path == '/Calls/' + CALL + '/Recordings/' + RECORDING + '.json':
            assert request.method == 'POST'
            assert parse_qs(request.content.decode()) == {'Status': ['stopped']}
            return httpx.Response(200, json={**self.row, 'status': 'stopped'})
        if path == '/Recordings/' + RECORDING + '.json':
            if request.method == 'DELETE':
                return httpx.Response(204)
            assert request.method == 'GET'
            return httpx.Response(200, json=self.row)
        if path == '/Recordings/' + RECORDING + '.wav':
            assert request.method == 'GET' and request.url.params['RequestedChannels'] == '2'
            return httpx.Response(self.media_status, content=self.media,
                                  headers={'Location': 'https://untrusted.example.test/audio'})
        pytest.fail('unexpected endpoint')


def test_import_and_language_replacement_have_no_io_or_default_mutation(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('import must not construct a client or load config')
    monkeypatch.setattr(httpx, 'AsyncClient', deny)
    monkeypatch.setattr(JpcallConfig, 'from_env', deny)
    importlib.reload(recording)
    original = CALL_LANGUAGES['ko']
    test_language = recording.owner_recording_language(context=context())
    assert test_language is not original and CALL_LANGUAGES['ko'] is original
    assert '녹음 없이' in original.instructions and '녹음 없이' not in test_language.instructions
    assert 'Twilio' in test_language.instructions and '녹음 기능이 작동하면' in test_language.instructions
    assert test_language.opener == '안녕하세요, AI 프리예요. 동의하신 녹음 테스트, 시작해도 될까요?'
    assert len(test_language.opener) <= 45
    # Consent is classified only by the owner-specific classifier tool, which grants nothing unclear.
    assert '명확히 동의하면' not in test_language.instructions
    assert 'recording test' in test_language.permission_description
    assert 'unclear and grants nothing' in test_language.permission_description
    assert test_language.hold == '잠시만요, 확인할게요.'
    assert test_language.farewell == original.farewell
    tree = ast.parse(Path(recording.__file__).read_text())
    assert not any(isinstance(node, ast.Attribute) and node.attr == 'create_call'
                   for node in ast.walk(tree))
    assert not any(isinstance(node, ast.If) and '__name__' in ast.unparse(node.test)
                   for node in tree.body)


def test_run_local_recording_notice_reaches_confirmed_brief_and_real_engine(tmp_path, monkeypatch):
    from test_engine_config import environment
    from test_engine_flow import CallAudio, CallEvents, make_engine, request, settled
    from test_engine_permission import notice
    from test_engine_transport import EVENTS

    async def run():
        original = CALL_LANGUAGES['ko']
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            profile = OwnerConfig.from_env(environment() | {
                'FRI_DATA': str(tmp_path), 'JPCALL_CALL_LANGUAGE': 'ko',
                'JPCALL_ALLOWED_USER_IDS': '12', 'JPCALL_TEST_NUMBERS': DESTINATION,
                'JPCALL_MAX_CALL_S': '120'})
            engine.config = engine._twilio.config = profile
            req = request(number_e164=DESTINATION)
            brief = await engine.plan(req)
            assert brief.opener_ja == profile.language.opener
            assert brief.opener_ja == '안녕하세요, AI 프리예요. 동의하신 녹음 테스트, 시작해도 될까요?'
            audio, events = CallAudio(provider.order), CallEvents(provider.order)
            call = await engine.start(brief, req, audio, events)
            await settled(lambda: bool(call._sid))
            media = await notice(call)
            await media.finish_playback()
            assert peer.responses[0]['instructions'] == (  # Only the literal prefix and notice.
                profile.language.say_prefix + json.dumps(brief.opener_ja, ensure_ascii=False))
            await call._rt.handle_event({'type': EVENTS['speech_started'],
                                        'item_id': 'owner-ready', 'audio_start_ms': 2000})
            await call._rt.handle_event({'type': EVENTS['speech_stopped'],
                                        'item_id': 'owner-ready', 'audio_end_ms': 2500})
            await call._rt.handle_event({'type': EVENTS['input_transcript'],
                                        'item_id': 'owner-ready', 'transcript': '네, 시작해 주세요.'})
            await settled(lambda: len(peer.responses) >= 2 and call._rt._idle.is_set())
            classifier = peer.responses[1]
            assert classifier['output_modalities'] == ['text']
            assert classifier['tool_choice'] == {'type': 'function', 'name': 'permission'}
            assert len(classifier['tools']) == 1
            assert 'owner' in classifier['tools'][0]['description']
            assert 'recording test' in classifier['tools'][0]['description']
            assert 'group listening' not in classifier['tools'][0]['description']
            await call._tool('permission', {'decision': 'granted'}, 'owner-start')
            await settled(lambda: call.publication_allowed and call._rt._idle.is_set())
            await media.finish_playback()
            await call._tool('ask_group', {'question_en': 'Which time should I ask for?',
                                         'question_original': '몇 시가 좋으세요?',
                                         'options': ['noon', 'one']}, 'missing-owner-fact')
            await settled(lambda: any(profile.language.hold in r.get('instructions', '')
                                      for r in peer.responses))
            hold_request = next(r['instructions'] for r in peer.responses
                                if profile.language.hold in r.get('instructions', ''))
            assert '일행분들께' not in hold_request
            instructions = call._rt.session_update()['session']['instructions']
            assert '녹음 없이' not in instructions and 'Twilio' in instructions
            question_tool = next(tool for tool in call._rt.session_update()['session']['tools']
                                 if tool['name'] == 'ask_group')
            assert 'owner' in question_tool['description']
            assert 'listening group' not in question_tool['description']
            assert 'the app asks the callee to wait' in question_tool['description']
            assert not any('/Recordings' in str(r.url) for r in provider.requests)
            assert CALL_LANGUAGES['ko'] is original and '녹음 없이' in original.instructions
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['absent', 'go', 'consent', 'owner', 'destination', 'sid',
                                  'language', 'duration', 'allowlist', 'account'])
def test_absent_or_wrong_owner_context_refuses_before_http(change):
    async def run():
        provider, ctx, cfg = Provider(), context(), config()
        if change == 'absent':
            ctx = None
        elif change in {'go', 'consent', 'owner'}:
            ctx = replace(ctx, **{{'go': 'explicit_go', 'consent': 'recording_consent',
                                  'owner': 'owner_only'}[change]: False})
        elif change == 'destination':
            ctx = replace(ctx, destination='+81312345678')
        elif change == 'sid':
            ctx = replace(ctx, call_sid='../../Calls.json')
        elif change == 'language':
            cfg = replace(cfg, call_language='ja')
        elif change == 'duration':
            cfg = replace(cfg, limits=replace(cfg.limits, max_call_s=121))
        elif change == 'allowlist':
            cfg = replace(cfg, limits=replace(cfg.limits, test_numbers=frozenset()))
        else:
            cfg = replace(cfg, twilio_account_sid='invalid')
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            api = TwilioClient(cfg, http)
            with pytest.raises(TwilioError):
                await recording.start_recording(api, context=ctx)
        assert provider.requests == []
    asyncio.run(run())


def test_start_only_records_exact_existing_call_and_never_creates_call():
    async def run():
        provider = Provider()
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            api = TwilioClient(config(), http)
            assert await recording.start_recording(api, context=context()) == RECORDING
        assert [request.method for request in provider.requests] == ['GET', 'GET', 'POST']
        assert parse_qs(provider.requests[-1].content.decode()) == {
            'RecordingChannels': ['dual'], 'RecordingTrack': ['both'], 'Trim': ['do-not-trim']}
        assert DESTINATION not in repr(context())
    asyncio.run(run())


@pytest.mark.parametrize(('field', 'value'), [('sid', 'CA' + '9' * 32), ('to', '+12025550124'),
    ('account_sid', 'AC' + '9' * 32), ('status', 'ringing'), ('status', 'completed'),
    ('direction', 'inbound')])
def test_authenticated_call_mismatch_blocks_record_start(field, value):
    async def run():
        provider = Provider()
        provider.call[field] = value
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with pytest.raises(TwilioError):
                await recording.start_recording(TwilioClient(config(), http), context=context())
        assert len(provider.requests) == 1 and provider.requests[0].method == 'GET'
    asyncio.run(run())


def test_existing_recording_and_provider_failure_do_not_retry_start():
    async def run():
        for prior in ([{'sid': RECORDING}], []):
            provider = Provider()
            provider.prior, provider.start_status = prior, 403
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
                with pytest.raises(TwilioError) as error:
                    await recording.start_recording(TwilioClient(config(), http), context=context())
            assert sum(request.method == 'POST' for request in provider.requests) == (0 if prior else 1)
            assert DESTINATION not in str(error.value) and 'synthetic-secret' not in str(error.value)
    asyncio.run(run())


def test_download_is_correlated_dual_wav_private_and_not_automatically_deleted(tmp_path):
    async def run():
        provider = Provider()
        provider.call['status'] = 'completed'
        provider.row['media_url'] = 'https://untrusted.example.test/audio?credential=do-not-follow'
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            api = TwilioClient(config(), http)
            path = await recording.download_completed(api, RECORDING, tmp_path, context=context())
        assert path.name == 'call.wav' and path.read_bytes() == provider.media
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert DESTINATION not in str(path) and not any(r.method == 'DELETE' for r in provider.requests)
        assert len(provider.requests) == 3
    asyncio.run(run())


@pytest.mark.parametrize('bad', ['account', 'call', 'recording', 'source', 'status', 'channels',
                                'duration', 'mono', 'empty', 'silent', 'truncated', 'redirect', 'size'])
def test_missing_or_wrong_recording_media_is_unavailable_and_leaves_no_artifact(tmp_path, bad):
    async def run():
        provider = Provider()
        if bad in {'account', 'call', 'recording'}:
            provider.row[{'account': 'account_sid', 'call': 'call_sid', 'recording': 'sid'}[bad]] = 'wrong'
        elif bad == 'source':
            provider.row['source'] = 'RecordVerb'
        elif bad == 'status':
            provider.row['status'] = 'processing'
        elif bad == 'channels':
            provider.row['channels'] = 1
        elif bad == 'duration':
            provider.row['duration'] = '121'
        elif bad == 'mono':
            provider.media = wav(channels=1)
        elif bad == 'empty':
            provider.media = b''
        elif bad == 'silent':
            provider.media = wav(silent=True)
        elif bad == 'truncated':
            provider.media = provider.media[:-100]
        elif bad == 'redirect':
            provider.media_status = 302
        else:
            provider.media = b'x' * (recording.MAX_AUDIO_BYTES + 1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with pytest.raises(TwilioError):
                await recording.download_completed(TwilioClient(config(), http), RECORDING, tmp_path,
                                                   context=context())
        assert not list(tmp_path.iterdir())
    asyncio.run(run())


def test_output_inside_git_and_invalid_sid_are_refused_before_http(tmp_path):
    async def run():
        (tmp_path / '.git').write_text('synthetic worktree marker')
        provider = Provider()
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with pytest.raises(TwilioError):
                await recording.download_completed(TwilioClient(config(), http), RECORDING, tmp_path,
                                                   context=context())
        assert provider.requests == []
    asyncio.run(run())


def test_stop_is_recording_only_and_delete_requires_separate_owner_review():
    async def run():
        provider = Provider()
        provider.row['status'] = 'processing'
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            api = TwilioClient(config(), http)
            await recording.stop_recording(api, RECORDING, context=context())
            before = len(provider.requests)
            with pytest.raises(TwilioError):
                await recording.delete_after_review(api, RECORDING, context=context())
            assert len(provider.requests) == before
            provider.row['status'] = 'completed'
            await recording.delete_after_review(api, RECORDING, context=context(), owner_reviewed=True)
        posts = [r for r in provider.requests if r.method == 'POST']
        assert len(posts) == 1 and '/Recordings/' in posts[0].url.path
        assert provider.requests[-1].method == 'DELETE'
    asyncio.run(run())


def test_stalled_provider_is_bounded_without_retry(monkeypatch):
    async def run():
        requests = []
        async def stall(request):
            requests.append(request)
            await asyncio.Future()
        monkeypatch.setattr(recording, 'TIMEOUT_S', .01)
        async with httpx.AsyncClient(transport=httpx.MockTransport(stall)) as http:
            async with asyncio.timeout(.5):
                with pytest.raises(TwilioError):
                    await recording.start_recording(TwilioClient(config(), http), context=context())
        assert len(requests) == 1
    asyncio.run(run())


def test_all_recording_operations_refuse_absent_context_before_io(tmp_path):
    async def run():
        provider = Provider()
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            api = TwilioClient(config(), http)
            for operation in (recording.stop_recording(api, RECORDING),
                              recording.download_completed(api, RECORDING, tmp_path),
                              recording.delete_after_review(api, RECORDING, owner_reviewed=True)):
                with pytest.raises(TwilioError):
                    await operation
        assert provider.requests == [] and not list(tmp_path.iterdir())
        with pytest.raises(TwilioError):
            recording.owner_recording_language()
    asyncio.run(run())


def test_one_silent_channel_and_overlong_media_do_not_pass_as_two_leg_capture(tmp_path):
    async def run():
        for media in (wav(channels=1), wav(frames=8000 * 121)):
            provider = Provider()
            provider.media = media
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
                with pytest.raises(TwilioError):
                    await recording.download_completed(TwilioClient(config(), http), RECORDING,
                                                       tmp_path, context=context())
        body = io.BytesIO()
        with wave.open(body, 'wb') as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\x01\x00\x00\x00' * 8000)
        provider = Provider()
        provider.media = body.getvalue()
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with pytest.raises(TwilioError):
                await recording.download_completed(TwilioClient(config(), http), RECORDING,
                                                   tmp_path, context=context())
        assert not list(tmp_path.iterdir())
    asyncio.run(run())


def test_stalled_media_download_is_bounded_and_creates_no_partial_file(monkeypatch, tmp_path):
    class Stall(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'RIFF'
            await asyncio.Future()

    async def run():
        provider = Provider()
        def handle(request):
            if request.url.path.endswith('.wav'):
                provider.requests.append(request)
                return httpx.Response(200, stream=Stall())
            return provider(request)
        monkeypatch.setattr(recording, 'TIMEOUT_S', .01)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with asyncio.timeout(.5):
                with pytest.raises(TwilioError):
                    await recording.download_completed(TwilioClient(config(), http), RECORDING,
                                                       tmp_path, context=context())
        assert len(provider.requests) == 3 and not list(tmp_path.iterdir())
    asyncio.run(run())


def test_lost_start_acknowledgement_is_never_automatically_retried():
    async def run():
        provider = Provider()
        posts = []
        def handle(request):
            if request.method == 'POST':
                posts.append(request)
                raise httpx.ReadTimeout('synthetic provider-private body', request=request)
            return provider(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with pytest.raises(TwilioError) as error:
                await recording.start_recording(TwilioClient(config(), http), context=context())
        assert len(posts) == 1 and 'provider-private' not in str(error.value)
    asyncio.run(run())


@pytest.mark.parametrize(('field', 'value'), [('channels', 1), ('status', 'absent'),
                                             ('source', 'RecordVerb')])
def test_start_response_does_not_claim_verified_capture_on_missing_tracks(field, value):
    async def run():
        provider = Provider()
        provider.row[field] = value
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with pytest.raises(TwilioError, match='not verified'):
                await recording.start_recording(TwilioClient(config(), http), context=context())
        assert sum(request.method == 'POST' for request in provider.requests) == 1
    asyncio.run(run())


def test_record_start_refuses_ordinary_no_recording_prompt_before_http():
    async def run():
        provider = Provider()
        ordinary = JpcallConfig(call_language='ko', twilio_account_sid=ACCOUNT,
                               limits=Limits(max_call_s=120, test_numbers=frozenset({DESTINATION})))
        assert '녹음 없이' in ordinary.language.instructions
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with pytest.raises(TwilioError, match='notice'):
                await recording.start_recording(TwilioClient(ordinary, http), context=context())
        assert provider.requests == []
    asyncio.run(run())
