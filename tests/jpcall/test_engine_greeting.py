"""The initial phone turn is gated by both greeting completion and signed AMD data."""

import asyncio
import socket

import pytest
from test_engine_flow import SID, PhoneMedia, dial, make_engine, settled

from fri.jpcall.contract import CallFinished, CallState, StatusChanged
from fri.jpcall.realtime import EVENTS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network connections are forbidden in engine tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


async def attach(call, clock):
    await call.status({'CallSid': SID, 'CallStatus': 'in-progress', 'SequenceNumber': '1'})
    media = PhoneMedia(call)
    await call.start(media)
    await settled(lambda: any(deadline <= clock.now + 4 and not future.done()
                               for deadline, future in clock.sleepers))
    return media


def hello_start(peer, item='hello'):
    peer.feed({'type': EVENTS['speech_started'], 'item_id': item, 'audio_start_ms': 0})


def hello_end(peer, item='hello'):
    peer.feed({'type': EVENTS['speech_stopped'], 'item_id': item, 'audio_end_ms': 500})


async def turns():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.parametrize('transcribed', [False, True])
def test_human_opener_waits_for_hello_to_finish(tmp_path, monkeypatch, transcribed):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await attach(call, clock)
            assert not peer.responses and not media.frames
            setup = peer.sent[0]['session']['audio']['input']['turn_detection']
            assert setup['create_response'] is False and setup['interrupt_response'] is False
            await call.status({'CallSid': SID, 'AnsweredBy': 'human'})
            hello_start(peer)
            await settled(lambda: call._greeting_item == 'hello')
            clock.advance(4)
            await turns()
            # Never interrupt an ongoing greeting, even past the fallback.
            assert not peer.responses
            if transcribed:
                peer.feed({'type': EVENTS['input_transcript'], 'item_id': 'hello',
                           'transcript': '여보세요', 'usage': {'type': 'duration', 'seconds': .5}})
            else:
                hello_end(peer)
            await settled(lambda: call._rt._conversation_open)
            assert len(peer.responses) == 1 and media.frames
            assert brief.opener_ja in peer.responses[0]['instructions']
            assert call._status_sequence == 1  # An AMD delivery never consumes a progress sequence.
            latest = [e for e in peer.sent if e['type'] == EVENTS['session_update']][-1]
            assert latest['session']['audio']['input']['turn_detection']['create_response'] is False
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('answered_by,deadline', [('human', 3), ('unknown', 3), (None, 4)])
def test_silent_callee_and_pending_amd_fallbacks(tmp_path, monkeypatch, answered_by, deadline):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await attach(call, clock)
            if answered_by:
                await call.status({'CallSid': SID, 'AnsweredBy': answered_by})
            clock.advance(deadline - .1)
            await turns()
            assert not peer.responses
            clock.advance(.1)
            await settled(lambda: call._rt._conversation_open)
            assert len(peer.responses) == 1
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_finished_hello_still_waits_for_amd_or_four_seconds(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await attach(call, clock)
            hello_start(peer)
            hello_end(peer)
            await settled(lambda: call._greeting_finished)
            clock.advance(3)
            await turns()
            assert not peer.responses
            clock.advance(1)
            await settled(lambda: call._rt._conversation_open)
            assert len(peer.responses) == 1
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('answered_by', [
    'machine_start', 'machine_end_beep', 'machine_end_silence', 'machine_end_other', 'fax',
])
def test_machine_hangs_up_without_speech(tmp_path, monkeypatch, answered_by):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await attach(call, clock)
            hello_start(peer)
            hello_end(peer)
            await call.status({'CallSid': SID, 'AnsweredBy': answered_by})
            await call.status({'CallSid': SID, 'AnsweredBy': answered_by})
            await asyncio.wait_for(call._finished.wait(), 1)
            assert not peer.responses and not media.frames and not audio.frames
            assert provider.hangups == 1 and peer.closed and audio.closed
            assert 'summary' not in provider.order
            failed = next(e for e in events.items if isinstance(e, StatusChanged)
                          and e.state == CallState.FAILED)
            finish = next(e for e in events.items if isinstance(e, CallFinished))
            assert failed.detail == finish.outcome_en == 'went to voicemail'
            amd, = [line for line in finish.usage if line.component == 'amd']
            assert amd.units == {'calls': 1} and amd.usd == .0075
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_machine_result_after_opener_ends_politely_and_reports_voicemail(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await attach(call, clock)
            hello_start(peer)
            hello_end(peer)
            clock.advance(4)
            await settled(lambda: call._rt._conversation_open)
            assert call._rt.has_spoken and len(peer.responses) == 1
            await call.status({'CallSid': SID, 'AnsweredBy': 'machine_start'})
            await asyncio.wait_for(call._finished.wait(), 1)
            assert len(peer.responses) == 2
            assert engine.config.language.farewell in peer.responses[-1]['instructions']
            assert provider.hangups == 1 and audio.closed
            finish = next(e for e in events.items if isinstance(e, CallFinished))
            assert finish.outcome_en == 'went to voicemail'
            assert any(isinstance(e, StatusChanged) and e.state == CallState.FAILED
                       and e.detail == 'went to voicemail' for e in events.items)
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_unsolicited_model_output_cannot_speak_before_opener(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await attach(call, clock)
            peer.feed({'type': EVENTS['response_created'], 'response': {'id': 'stray'}})
            peer.feed({'type': EVENTS['output_audio'], 'response_id': 'stray', 'item_id': 'stray1',
                       'content_index': 0, 'delta': '/w=='})
            peer.feed({'type': EVENTS['output_transcript'], 'response_id': 'stray',
                       'item_id': 'stray1', 'content_index': 0, 'transcript': 'Wrong first words'})
            await turns()
            assert not media.frames and not audio.frames and not call._transcript
            assert any(e['type'] == EVENTS['response_cancel'] for e in peer.sent)
            hello_start(peer)
            hello_end(peer)
            await call.status({'CallSid': SID, 'AnsweredBy': 'human'})
            await settled(lambda: call._rt._conversation_open)
            assert len(peer.responses) == 1 and brief.opener_ja in peer.responses[0]['instructions']
            assert media.frames
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_machine_before_media_attachment_never_opens(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await call.status({'CallSid': SID, 'AnsweredBy': 'machine_start'})
            await asyncio.wait_for(call._finished.wait(), 1)
            media = PhoneMedia(call)
            await call.start(media)
            assert media.closed and not peer.responses and provider.hangups == 1
            assert next(e for e in events.items if isinstance(e, CallFinished)).outcome_en == (
                'went to voicemail')
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_machine_result_after_opener_request_before_first_audio_stays_silent(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        original = peer.send_json

        async def deferred_opener(event):
            if event['type'] == EVENTS['response_create']:
                peer.sent.append(event)
                return  # The model has not yet acknowledged or produced any audio.
            await original(event)

        peer.send_json = deferred_opener
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await attach(call, clock)
            hello_start(peer)
            hello_end(peer)
            await call.status({'CallSid': SID, 'AnsweredBy': 'human'})
            await settled(lambda: any(e['type'] == EVENTS['response_create'] for e in peer.sent))
            command = next(e['response'] for e in peer.sent
                           if e['type'] == EVENTS['response_create'])
            resource = {'id': 'deferred', 'metadata': command['metadata']}
            await call.status({'CallSid': SID, 'AnsweredBy': 'machine_start'})
            # An already-requested opener can arrive after the machine callback; it stays fenced.
            peer.feed({'type': EVENTS['response_created'], 'response': resource})
            peer.feed({'type': EVENTS['output_audio'], 'response_id': 'deferred',
                       'item_id': 'deferred1', 'content_index': 0, 'delta': '/w=='})
            await asyncio.wait_for(call._finished.wait(), 1)
            assert not media.frames and not audio.frames and provider.hangups == 1
            assert not any(engine.config.language.farewell in e.get('response', {}).get(
                'instructions', '') for e in peer.sent)
            assert next(e for e in events.items if isinstance(e, CallFinished)).outcome_en == (
                'went to voicemail')
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_failed_opener_never_enables_automatic_replies(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        original = peer.send_json

        async def failed_opener(event):
            if event['type'] == EVENTS['response_create']:
                peer.sent.append(event)
                resource = {'id': 'failed-opening', 'metadata': event['response']['metadata'],
                            'status': 'failed', 'usage': None, 'output': []}
                peer.feed({'type': EVENTS['response_created'], 'response': resource})
                peer.feed({'type': EVENTS['response_done'], 'response': resource})
                return
            await original(event)

        peer.send_json = failed_opener
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await attach(call, clock)
            await call.status({'CallSid': SID, 'AnsweredBy': 'human'})
            hello_start(peer)
            hello_end(peer)
            await asyncio.wait_for(call._finished.wait(), 1)
            assert not media.frames and provider.hangups == 1
            assert not any(e['session']['audio']['input']['turn_detection']['create_response']
                           for e in peer.sent if e['type'] == EVENTS['session_update'])
            assert any(isinstance(e, StatusChanged) and e.state == CallState.FAILED
                       for e in events.items)
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())
