"""Consent needs the complete played notice; its response time starts after that playback."""

import asyncio
import json
from dataclasses import replace

import pytest
from test_engine_flow import SID, PhoneMedia, dial, make_engine, settled
from test_engine_speech_regressions import callee, output, turns
from test_engine_transport import EVENTS, Media, bridge


class DelayedPhone(PhoneMedia):
    def __init__(self, call):
        super().__init__(call)
        self.marks = []

    async def mark(self, name):
        self.marks.append(name)

    async def finish_playback(self):
        for mark in self.marks:
            await self.call.mark(mark)
        self.marks.clear()
        for _ in range(20):
            await asyncio.sleep(0)


async def notice(call):
    await call.status({'CallSid': SID, 'CallStatus': 'in-progress', 'SequenceNumber': '1'})
    media = DelayedPhone(call)
    await call.start(media)
    await call.status({'CallSid': SID, 'AnsweredBy': 'human'})
    await call._rt.handle_event({'type': EVENTS['speech_started'], 'item_id': 'hello', 'audio_start_ms': 0})
    await call._rt.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'hello'})
    await settled(lambda: bool(media.marks) and call._rt._conversation_open)
    return media


async def turn_clock(clock, seconds):
    clock.advance(seconds)
    for _ in range(30):
        await asyncio.sleep(0)


def test_permission_window_starts_after_notice_phone_playback(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, call_language='ko')
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)
            await turn_clock(clock, 18)
            await media.finish_playback()
            await turn_clock(clock, 2)
            assert not call._ending  # The answer-time deadline must not consume the response window.
            await turn_clock(clock, 17)
            assert not call._ending
            await turn_clock(clock, 1)
            await settled(lambda: call._ending)
            assert call._detail == 'Permission to share the call was not established.'
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_notice_playback_itself_has_a_bounded_deadline(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await notice(call)
            await turn_clock(clock, 29)
            assert not call._ending
            await turn_clock(clock, 1)
            await settled(lambda: call._ending)
            assert call._detail == 'The call notice could not be delivered.'
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_transfer_never_inherits_an_old_permission_deadline(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)
            await media.finish_playback()
            await call._handle_tool('permission', {'decision': 'granted'}, 'initial-consent')
            await settled(lambda: call._rt._idle.is_set())
            await media.finish_playback()
            await turn_clock(clock, 10)
            previous = call._hold
            await call._tool('permission', {'decision': 'transfer'}, 'new-person')
            await settled(lambda: call._hold is not previous and call._rt._conversation_open and bool(media.marks))
            await turn_clock(clock, 10)
            assert not call._ending
            await media.finish_playback()
            await turn_clock(clock, 19)
            assert not call._ending
            await turn_clock(clock, 1)
            await settled(lambda: call._ending)
            assert call._detail == 'Permission to share the call was not established.'
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_stale_notice_drain_timeout_cannot_end_a_new_permission_phase(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            entered, release = asyncio.Event(), asyncio.Event()

            async def notice_delivery(*, timeout_s):
                entered.set()
                await release.wait()
                raise TimeoutError

            monkeypatch.setattr(call._rt, 'notice_delivery', notice_delivery)
            task = call._spawn(call._notice_delivered(call.consent_generation))
            await entered.wait()
            call._permission(True)
            call._permission(False)
            release.set()
            await task
            await asyncio.sleep(0)
            assert not call._ending
            assert call._notice_delivered_generation is None
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


async def reply(call, item, text):
    await call._rt.handle_event({'type': EVENTS['speech_started'], 'item_id': item,
                                'audio_start_ms': 5000})
    await call._rt.handle_event({'type': EVENTS['speech_stopped'], 'item_id': item,
                                'audio_end_ms': 5600})
    await call._rt.handle_event({'type': EVENTS['input_transcript'], 'item_id': item,
                                'transcript': text})


def decide(peer, *decisions):
    """Answer each tool-only classifier request with the next decision, as the model would."""
    original, queue = peer.send_json, list(decisions)

    async def send_json(event):
        if (event['type'] == EVENTS['response_create'] and queue
                and event['response'].get('output_modalities') == ['text']):
            peer.sent.append(event)
            peer.responses.append(event['response'])
            number = event['response']['metadata']['jpcall_command']
            resource = {'id': 'classifier-' + number, 'metadata': event['response']['metadata'],
                        'status': 'completed', 'usage': None, 'output': [{
                            'type': 'function_call', 'status': 'completed', 'name': 'permission',
                            'call_id': 'decision-' + number,
                            'arguments': json.dumps({'decision': queue.pop(0)})}]}
            peer.feed({'type': EVENTS['response_created'], 'response': resource})
            peer.feed({'type': EVENTS['response_done'], 'response': resource})
        else:
            await original(event)
    peer.send_json = send_json


def classifiers(peer):
    return [r for r in peer.responses if r.get('output_modalities') == ['text']]


def test_reply_over_the_notice_never_grants_and_gets_one_complete_repeat(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)  # Generated; the phone has not acknowledged playback.
            generation = call.consent_generation
            decide(peer, 'granted')
            first = len(peer.responses)
            await reply(call, 'over-notice', 'はい')
            await settled(lambda: len(peer.responses) == first + 1 and call._rt._conversation_open)
            repeat = peer.responses[-1]
            assert brief.opener_ja in repeat['instructions']
            assert repeat.get('output_modalities') != ['text'] and not classifiers(peer)
            assert [e['audio_end_ms'] for e in peer.sent if e['type'] == EVENTS['truncate']] == [0]
            assert call._notice_delivered_generation is None
            await call._handle_tool('permission', {'decision': 'granted'}, 'synthetic-early')
            assert not call.publication_allowed and not call._ending
            await reply(call, 'over-repeat', 'はい')  # The single repeat is never cut or repeated.
            await turn_clock(clock, 0)
            assert len(peer.responses) == first + 1
            assert len([e for e in peer.sent if e['type'] == EVENTS['truncate']]) == 1
            await media.finish_playback()  # Also acknowledges the cleared notice's stale mark.
            await settled(lambda: call._notice_delivered_generation == generation)
            await reply(call, 'after-notice', 'はい、どうぞ。')
            await settled(lambda: call.publication_allowed)
            assert len(classifiers(peer)) == 1
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_grant_needs_the_current_notice_after_transfer(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)
            await call._handle_tool('permission', {'decision': 'granted'}, 'before-playback')
            assert not call.publication_allowed
            await media.finish_playback()
            await call._handle_tool('permission', {'decision': 'granted'}, 'after-playback')
            assert call.publication_allowed
            await settled(lambda: call._rt._idle.is_set())
            await media.finish_playback()
            previous = call.consent_generation
            await call._tool('permission', {'decision': 'transfer'}, 'new-person')
            await settled(lambda: call._rt._conversation_open and bool(media.marks))
            assert call.consent_generation > previous
            assert call._notice_delivered_generation != call.consent_generation
            await call._handle_tool('permission', {'decision': 'granted'}, 'old-notice')
            assert not call.publication_allowed
            await media.finish_playback()
            await settled(lambda: call._notice_delivered_generation == call.consent_generation)
            decide(peer, 'granted')
            await reply(call, 'new-person-reply', 'はい、どうぞ。')
            await settled(lambda: call.publication_allowed)
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_unclear_reply_grants_nothing_repeats_once_and_keeps_the_deadline(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)
            await media.finish_playback()
            generation = call.consent_generation
            await settled(lambda: call._notice_delivered_generation == generation)
            decide(peer, 'unclear', 'unclear')
            first = len(peer.responses)
            await turn_clock(clock, 2)
            await reply(call, 'question', 'どういったご用件ですか？')
            await settled(lambda: len(peer.responses) == first + 2 and call._rt._conversation_open)
            assert peer.responses[first]['tool_choice'] == {'type': 'function', 'name': 'permission'}
            assert brief.opener_ja in peer.responses[first + 1]['instructions']
            assert not call.publication_allowed and not call._ending
            assert call.consent_generation == generation
            outputs = [json.loads(e['item']['output']) for e in peer.sent
                       if e['type'] == EVENTS['item_create']]
            assert outputs == [{'permission': 'unclear'}]
            await media.finish_playback()
            await reply(call, 'again', 'えっ？')
            await settled(lambda: len(peer.responses) == first + 3)
            await turn_clock(clock, 0)
            assert len(peer.responses) == first + 3  # No second repeat in this notice phase.
            await turn_clock(clock, 17)
            assert not call._ending
            await turn_clock(clock, 1)  # Twenty seconds after the first delivery, as before.
            await settled(lambda: call._ending)
            assert call._detail == 'Permission to share the call was not established.'
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_debug_timeline_keeps_timing_but_text_only_under_current_capture_consent(
        tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        path = tmp_path / 'timeline.jsonl'
        engine.config = replace(engine.config, debug_timeline_path=path)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)
            first = len(peer.responses)
            await reply(call, 'pre-consent', '録音はしないでください。')
            await settled(lambda: len(peer.responses) == first + 1 and call._rt._conversation_open)
            await media.finish_playback()
            await settled(lambda: call._notice_delivered_generation == call.consent_generation)
            decide(peer, 'granted')
            await reply(call, 'consent', 'はい、どうぞ。')
            await settled(lambda: call.publication_allowed)
            await settled(lambda: call._rt._idle.is_set())
            await reply(call, 'admitted', '七時なら空いています。')
            await settled(lambda: call._rt._idle.is_set())
            await call._rt.handle_event({'type': EVENTS['speech_started'], 'item_id': 'delayed',
                                        'audio_start_ms': 9000})
            peer.tool('permission', {'decision': 'transfer'}, 'transfer')  # Revokes publication.
            await settled(lambda: not call.publication_allowed)
            await call._rt.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'delayed',
                                        'transcript': '先ほどの件は内密に。'})
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            spoken = {row['item_id']: row for row in rows if row['event'] == 'transcript'}
            assert {'pre-consent', 'consent', 'admitted', 'delayed'} <= set(spoken)
            assert spoken['admitted']['text'] == '七時なら空いています。'
            for item in ('pre-consent', 'consent', 'delayed'):
                assert 'text' not in spoken[item] and spoken[item]['at']
            raw = path.read_text(encoding='utf-8')
            for private in ('録音はしないでください', '先ほどの件は内密に'):
                assert private not in raw
            decisions = [row for row in rows if row['event'] == 'tool_call']
            assert decisions and 'arguments' not in decisions[0]  # Pre-consent classifier call.
            assert any(row['event'] == 'tool_result' and row.get('result') == {
                'permission': 'granted'} for row in rows)
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def requests(peer):
    return [e['response'] for e in peer.sent if e['type'] == EVENTS['response_create']]


@pytest.mark.parametrize('case', ['zero', 'partial', 'cleared', 'complete'])
def test_only_a_complete_acknowledged_notice_lets_a_reply_be_classified(case):
    async def run():
        client, audio, events, meter, peer = bridge()
        await client.start(Media())
        await client.open_conversation()
        try:
            notice = client.brief.opener_ja
            played = {'zero': 0, 'partial': 1, 'cleared': 1, 'complete': 2}[case]
            await output(client, response='command-1', item='notice', text=notice, chunks=2,
                         played=played)
            if case == 'cleared':
                echoed = list(client._marks)
                await client._barge_in()
                for name in echoed:  # Twilio acknowledges cleared marks too.
                    await client.mark(name)
            before = len(requests(peer))
            await callee(client, 'reply', 'はい、どうぞ。', start=5000, stop=5600)
            await turns()
            new = requests(peer)[before:]
            assert len(new) == 1
            if case == 'complete':
                assert new[0]['output_modalities'] == ['text']
                assert new[0]['tool_choice'] == {'type': 'function', 'name': 'permission'}
                assert 'unclear' in new[0]['instructions']
            else:  # Never classified: one repeat of the unchanged notice instead.
                assert new[0].get('output_modalities') != ['text']
                assert notice in new[0]['instructions']
        finally:
            await client.stop()
    asyncio.run(run())


def test_only_a_classifier_of_the_current_delivered_notice_can_decide():
    async def run():
        decided = []

        async def tool(name, arguments, call_id):
            decided.append(call_id)

        client, audio, events, meter, peer = bridge(on_tool_call=tool)
        await client.start(Media())
        await client.open_conversation()
        try:
            notice = client.brief.opener_ja
            await output(client, response='command-1', item='notice', text=notice, played=1)

            def granted(call_id):
                return {'type': 'function_call', 'status': 'completed', 'name': 'permission',
                        'call_id': call_id, 'arguments': json.dumps({'decision': 'granted'})}
            await client.handle_event({'type': EVENTS['response_done'], 'response': {
                'id': 'command-1', 'status': 'completed', 'output': [granted('inside-notice')]}})
            held, original = [], peer.send_json

            async def hold_classifier(event):
                if (event['type'] == EVENTS['response_create']
                        and event['response'].get('output_modalities') == ['text']):
                    peer.sent.append(event)
                    held.append(event['response']['metadata'])
                else:
                    await original(event)
            peer.send_json = hold_classifier
            await callee(client, 'reply', 'はい、どうぞ。', start=5000, stop=5600)
            await turns()
            stale = {'id': 'stale', 'metadata': held[0], 'status': 'completed',
                     'output': [granted('stale')]}
            await client.handle_event({'type': EVENTS['response_created'], 'response': stale})
            renotice = asyncio.create_task(client.repeat_notice())  # A newer notice begins.
            await turns()
            await client.handle_event({'type': EVENTS['response_done'], 'response': stale})
            await asyncio.wait_for(renotice, 1)
            assert decided == []
            number = requests(peer)[-1]['metadata']['jpcall_command']
            await output(client, response='command-' + number, item='renewed', text=notice,
                         played=1)
            await callee(client, 'fresh', 'はい、どうぞ。', start=9000, stop=9600)
            await turns()
            fresh = {'id': 'fresh', 'metadata': held[1], 'status': 'completed',
                     'output': [granted('fresh')]}
            await client.handle_event({'type': EVENTS['response_created'], 'response': fresh})
            await client.handle_event({'type': EVENTS['response_done'], 'response': fresh})
            assert decided == ['fresh']
        finally:
            await client.stop()
    asyncio.run(run())
