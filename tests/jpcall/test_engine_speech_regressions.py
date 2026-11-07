"""Synthetic speech/permission/playout oracles; no provider connections."""

import asyncio
import base64

import pytest
from test_engine_transport import EVENTS, ULAW, Media, attach_and_open, bridge

import fri.jpcall.realtime as realtime


async def turns():
    for _ in range(20):
        await asyncio.sleep(0)


async def output(client, item='fri1', response='command-1', text='六名様でよろしいですか。',
                 *, chunks=1, played=0, finished=True):
    for _ in range(chunks):
        await client.handle_event({'type': EVENTS['output_audio'], 'response_id': response,
            'item_id': item, 'content_index': 0, 'delta': base64.b64encode(ULAW).decode()})
    if finished:
        await client.handle_event({'type': EVENTS['output_audio_done'], 'response_id': response,
            'item_id': item, 'content_index': 0})
    await client.handle_event({'type': EVENTS['output_transcript'], 'response_id': response,
        'item_id': item, 'content_index': 0, 'transcript': text})
    for mark in list(client._marks)[:played]:
        await client.mark(mark)


async def callee(client, item, text, *, start=100, stop=200):
    await client.handle_event({'type': EVENTS['speech_started'], 'item_id': item,
                              'audio_start_ms': start})
    await client.handle_event({'type': EVENTS['speech_stopped'], 'item_id': item,
                              'audio_end_ms': stop})
    await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': item,
                              'transcript': text})


@pytest.mark.parametrize('spoken,answer', [
    ('예약은 내일 저녁 여섯 시에 가능할까요?', '예약은 내일 저녁 일곱 시에 가능할까요'),
    ('六名様でよろしいですか。', '六名ではなく七名です。'),
    ('明日の予約は可能でしょうか。', '明日の予約は不可能です。'),
])
def test_corrections_are_retained_and_receive_a_response(spoken, answer):
    async def run():
        client, audio, events, meter, peer = bridge()
        await attach_and_open(client, Media())
        try:
            await output(client, text=spoken, played=1)
            before = sum(e['type'] == EVENTS['response_create'] for e in peer.sent)
            await callee(client, 'correction', answer)
            await turns()
            row = next(e for e in events.items if e.item_id == 'correction')
            assert row.text_ja == answer and row.delivery == 'complete'
            assert (row.started_at, row.ended_at) == (.1, .2)
            assert not any(e['type'] == EVENTS['item_delete'] for e in peer.sent)
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == before + 1
        finally:
            await client.stop()
    asyncio.run(run())


def test_exact_echo_is_retained_uncertain_and_repeated_answer_has_bounded_recovery(monkeypatch):
    monkeypatch.setattr(realtime, 'ASR_TIMEOUT_S', .02)

    async def run():
        exhausted = asyncio.Event()

        async def failure():
            exhausted.set()

        client, audio, events, meter, peer = bridge(on_asr_failure=failure)
        await attach_and_open(client, Media())
        try:
            text = 'お客様は１２時の予約をご希望です。'
            await output(client, text=text, played=1)
            for item in ('repeat1', 'repeat2'):
                await callee(client, item, 'お客様は12時の予約をご希望です')
            await asyncio.wait_for(exhausted.wait(), 1)
            rows = [e for e in events.items if e.speaker == 'callee']
            assert len(rows) == 2 and all(e.delivery == 'unknown' for e in rows)
            requests = [e for e in peer.sent if e['type'] == EVENTS['response_create']]
            assert len(requests) == 2  # Opening plus one clarification, no echo loop.
            assert 'could not hear' in requests[-1]['response']['instructions']
            assert not any(e['type'] == EVENTS['item_delete'] for e in peer.sent)
            assert client._silenced
        finally:
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['missing', 'empty', 'failed'])
def test_missing_empty_and_failed_asr_clarify_once_then_end(monkeypatch, failure):
    monkeypatch.setattr(realtime, 'ASR_TIMEOUT_S', .02)

    async def run():
        exhausted = asyncio.Event()

        async def failed():
            exhausted.set()

        client, audio, events, meter, peer = bridge(on_asr_failure=failed)
        await attach_and_open(client, Media())
        try:
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'lost',
                                       'audio_start_ms': 100})
            await client.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'lost',
                                       'audio_end_ms': 200})
            if failure != 'missing':
                await client.handle_event({'type': EVENTS[
                    'input_transcript' if failure == 'empty' else 'input_transcript_failed'],
                    'item_id': 'lost', 'transcript': ''})
            await asyncio.wait_for(exhausted.wait(), 1)
            assert not events.items
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == 2
        finally:
            await client.stop()
    asyncio.run(run())


def test_valid_asr_after_clarification_recovers_without_ending(monkeypatch):
    monkeypatch.setattr(realtime, 'ASR_TIMEOUT_S', .03)

    async def run():
        exhausted = []

        async def failure():
            exhausted.append(True)

        client, audio, events, meter, peer = bridge(on_asr_failure=failure)
        await attach_and_open(client, Media())
        try:
            await client.handle_event({'type': EVENTS['input_transcript_failed'], 'item_id': 'x'})
            await turns()
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == 2
            await callee(client, 'answer', '七名です。')
            await asyncio.sleep(.05)
            assert not exhausted and not client._silenced
            assert next(e for e in events.items if e.item_id == 'answer').delivery == 'complete'
        finally:
            await client.stop()
    asyncio.run(run())


def test_reply_during_opening_is_released_but_initial_greeting_is_not():
    async def run():
        client, audio, events, meter, peer = bridge()
        await client.start(Media())
        await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'hello',
                                   'audio_start_ms': 0})
        original = peer.send_json
        opening = []

        async def defer(event):
            if event['type'] == EVENTS['response_create'] and not opening:
                peer.sent.append(event)
                opening.append(event['response']['metadata'])
            else:
                await original(event)

        peer.send_json = defer
        task = asyncio.create_task(client.open_conversation())
        try:
            await turns()
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'hello',
                                       'transcript': 'もしもし。'})
            await callee(client, 'permission-reply', 'はい、どうぞ。')
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == 1
            response = {'id': 'opening', 'metadata': opening[0], 'status': 'completed',
                        'output': []}
            await client.handle_event({'type': EVENTS['response_created'], 'response': response})
            await client.handle_event({'type': EVENTS['response_done'], 'response': response})
            await task
            await turns()
            requests = [e for e in peer.sent if e['type'] == EVENTS['response_create']]
            assert len(requests) == 2
            assert requests[-1]['response']['output_modalities'] == ['text']
            assert requests[-1]['response']['tool_choice']['name'] == 'permission'
            assert all(e['type'] != EVENTS['item_delete'] for e in peer.sent)
        finally:
            task.cancel()
            await client.stop()
    asyncio.run(run())


def test_pregrant_response_is_tool_only_and_inquiry_audio_and_tools_are_fenced():
    async def run():
        calls = []

        async def tool(name, args, call_id):
            calls.append(name)

        client, audio, events, meter, peer = bridge(on_tool_call=tool, tools=(
            {'type': 'function', 'name': 'permission'}, {'type': 'function', 'name': 'ask_group'}))
        await client.start(Media())
        await client.open_conversation()
        try:
            await callee(client, 'permission-reply', 'はい。')
            await turns()
            request = [e for e in peer.sent if e['type'] == EVENTS['response_create']][-1]
            assert request['response']['output_modalities'] == ['text']
            assert [tool['name'] for tool in request['response']['tools']] == ['permission']
            await output(client, response='command-2', item='unauthorized')
            assert not audio.frames
            resource = {'id': 'command-2', 'status': 'completed', 'output': [
                {'type': 'function_call', 'status': 'completed', 'name': name,
                 'call_id': name, 'arguments': '{"decision":"granted"}'}
                for name in ('ask_group', 'permission')]}
            await client.handle_event({'type': EVENTS['response_done'], 'response': resource})
            assert calls == ['permission']
            client.set_permission(True)
            await output(client, response='command-2', item='still-unauthorized')
            assert not audio.frames  # A pregrant response cannot gain audio authority later.
            await client.audio(ULAW)  # Permission recognition still receives callee audio.
            assert peer.sent[-1]['type'] == EVENTS['input_append']
        finally:
            await client.stop()
    asyncio.run(run())


def test_revocation_fences_queued_and_completed_old_response_after_regrant():
    async def run():
        client, audio, events, meter, peer = bridge()
        media = Media()
        await attach_and_open(client, media)
        try:
            await client.handle_event({'type': EVENTS['response_created'],
                                       'response': {'id': 'busy'}})
            pending = asyncio.create_task(client.say('秘密を伝えます。'))
            await turns()
            before = sum(e['type'] == EVENTS['response_create'] for e in peer.sent)
            client.set_permission(False)
            client.set_permission(True)
            generated = await asyncio.wait_for(pending, 1)
            assert await generated == 'cancelled'
            await output(client, response='command-1', item='stale-opening')
            await output(client, response='busy', item='stale-inquiry')
            assert not audio.frames
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == before
            assert ('clear',) in media.sent
        finally:
            await client.stop()
    asyncio.run(run())


def test_revoke_during_phone_write_clears_it_and_does_not_relay_to_discord():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowMedia(Media):
            async def send_audio(self, data):
                entered.set()
                await release.wait()
                await super().send_audio(data)

        client, audio, events, meter, peer = bridge()
        media = SlowMedia()
        await attach_and_open(client, media)
        task = asyncio.create_task(output(client))
        try:
            await entered.wait()
            client.set_permission(False)
            release.set()
            await task
            await turns()
            assert not audio.frames
            assert media.sent[-1] == ('clear',)
            assert not client._marks
        finally:
            release.set()
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('played,delivery', [(0, None), (1, 'interrupted')])
def test_unplayed_or_partial_text_never_claims_complete_speech(played, delivery):
    async def run():
        completed = []
        client, audio, events, meter, peer = bridge(on_callee_response=lambda: completed.append(True))
        await attach_and_open(client, Media())
        try:
            generated = await client.say('料金を承諾します。')
            await generated
            await output(client, response='command-2', chunks=2, played=played,
                         text='料金を承諾します。予約を確定します。')
            await callee(client, 'interrupt', '承諾していません。')
            rows = [e for e in events.items if e.speaker == 'fri']
            assert not rows if delivery is None else len(rows) == 1
            if rows:
                assert rows[0].delivery == delivery
                assert not rows[0].text_ja.endswith(' …')
            assert not completed
        finally:
            await client.stop()
    asyncio.run(run())


def test_delivered_reply_callback_requires_completed_generation_and_all_phone_marks():
    async def run():
        completed = []
        client, audio, events, meter, peer = bridge(on_callee_response=lambda: completed.append(True))
        await attach_and_open(client, Media())
        try:
            generated = await client.say('確認しています。')
            await generated
            await output(client, response='command-2', chunks=2, played=1)
            assert not completed and not events.items
            await client.mark('2')
            assert completed == [True]
            assert events.items[0].delivery == 'complete'
            await client.mark('2')
            assert completed == [True]
        finally:
            await client.stop()
    asyncio.run(run())
