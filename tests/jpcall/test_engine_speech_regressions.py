"""Synthetic speech/permission/playout oracles; no provider connections."""

import asyncio
import base64
import json

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
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'x',
                                       'audio_start_ms': 100})
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


def test_reply_during_opening_is_not_classified_and_initial_greeting_is_ignored():
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
            await output(client, response='opening', item='notice', text=client.brief.opener_ja,
                         played=1)
            await turns()
            requests = [e for e in peer.sent if e['type'] == EVENTS['response_create']]
            assert len(requests) == 1  # The reply made during the opening is never classified.
            await callee(client, 'after-notice', 'はい、どうぞ。', start=5000, stop=5600)
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
            await output(client, response='command-1', item='notice', text=client.brief.opener_ja,
                         played=1)
            notice_frames = len(audio.frames)
            await callee(client, 'permission-reply', 'はい。', start=5000, stop=5600)
            await turns()
            request = [e for e in peer.sent if e['type'] == EVENTS['response_create']][-1]
            assert request['response']['output_modalities'] == ['text']
            assert [tool['name'] for tool in request['response']['tools']] == ['permission']
            await output(client, response='command-2', item='unauthorized')
            assert len(audio.frames) == notice_frames
            resource = {'id': 'command-2', 'status': 'completed', 'output': [
                {'type': 'function_call', 'status': 'completed', 'name': name,
                 'call_id': name, 'arguments': '{"decision":"granted"}'}
                for name in ('ask_group', 'permission')]}
            await client.handle_event({'type': EVENTS['response_done'], 'response': resource})
            assert calls == ['permission']
            client.set_permission(True)
            await output(client, response='command-2', item='still-unauthorized')
            assert len(audio.frames) == notice_frames  # No audio authority gained later.
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


@pytest.mark.parametrize('initial,duplicate_start', [(None, False), (None, True), (1, False)])
def test_late_asr_keeps_capture_consent_instead_of_current_permission(initial, duplicate_start):
    async def run():
        context = [initial]
        client, audio, events, meter, peer = bridge(publication_context=lambda: context[0])
        await attach_and_open(client, Media())
        try:
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'late',
                                       'audio_start_ms': 100})
            client.set_permission(False)
            context[0] = 3
            client.set_permission(True)
            if duplicate_start:
                await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'late',
                                           'audio_start_ms': 100})
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'late',
                                       'transcript': '七名です。'})
            row = next(e for e in events.items if e.item_id == 'late')
            assert row.consent_generation == initial
            assert row.consent_generation != context[0]
        finally:
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('capture,callback', [(False, True), (True, False)])
def test_missing_capture_or_publication_callback_is_not_consent_evidence(capture, callback):
    async def run():
        client, audio, events, meter, peer = bridge(
            publication_context=(lambda: 7) if callback else None)
        await attach_and_open(client, Media())
        try:
            if capture:
                await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'x',
                                           'audio_start_ms': 100})
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'x',
                                       'transcript': '七名です。'})
            assert events.items[-1].consent_generation is None
        finally:
            await client.stop()
    asyncio.run(run())


def test_fri_transcript_keeps_command_creation_consent_across_queue_wait():
    async def run():
        context = [1]
        client, audio, events, meter, peer = bridge(publication_context=lambda: context[0])
        await attach_and_open(client, Media())
        pending = None
        try:
            await client.handle_event({'type': EVENTS['response_created'],
                                       'response': {'id': 'busy'}})
            pending = asyncio.create_task(client.say('確認しています。'))
            await turns()
            context[0] = 3
            await client.handle_event({'type': EVENTS['response_done'], 'response': {
                'id': 'busy', 'status': 'completed', 'output': []}})
            generated = await pending
            await generated
            await output(client, response='command-2', item='late-fri', played=1)
            row = next(e for e in events.items if e.item_id == 'late-fri')
            assert row.consent_generation == 1
        finally:
            if pending is not None:
                pending.cancel()
            await client.stop()
    asyncio.run(run())


def test_uncorrelated_fri_output_has_no_publication_evidence():
    async def run():
        client, audio, events, meter, peer = bridge(publication_context=lambda: 7)
        await attach_and_open(client, Media())
        try:
            await client.handle_event({'type': EVENTS['response_created'],
                                       'response': {'id': 'uncorrelated'}})
            await output(client, response='uncorrelated', item='unknown-source', played=1)
            assert events.items[-1].consent_generation is None
        finally:
            await client.stop()
    asyncio.run(run())


def test_transfer_notice_speaks_before_tool_only_classification_of_the_next_reply():
    async def run():
        client, audio, events, meter, peer = bridge()
        media = Media()
        await attach_and_open(client, media)
        try:
            notice = client.brief.opener_ja
            client.set_permission(False)
            before = sum(e['type'] == EVENTS['response_create'] for e in peer.sent)
            await client.repeat_notice()
            request = [e for e in peer.sent if e['type'] == EVENTS['response_create']][-1]
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == before + 1
            assert request['response'].get('output_modalities') != ['text']
            assert notice in request['response']['instructions']
            await output(client, response='command-2', item='renewed-notice', text=notice, played=1)
            assert len(audio.frames) == 1 and not client._permission
            await callee(client, 'new-recipient', 'はい、どうぞ。')
            await turns()
            request = [e for e in peer.sent if e['type'] == EVENTS['response_create']][-1]
            assert request['response']['output_modalities'] == ['text']
            assert request['response']['tool_choice']['name'] == 'permission'
        finally:
            await client.stop()
    asyncio.run(run())


def test_pending_reply_does_not_cross_a_permission_change():
    async def run():
        client, audio, events, meter, peer = bridge()
        await attach_and_open(client, Media())
        try:
            await client.handle_event({'type': EVENTS['response_created'],
                                       'response': {'id': 'busy'}})
            await callee(client, 'old', '七名です。')
            before = sum(e['type'] == EVENTS['response_create'] for e in peer.sent)
            client.set_permission(False)
            await client.repeat_notice()
            await turns()
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == before + 1
            assert not client._permission
            renewed = [e for e in peer.sent if e['type'] == EVENTS['response_create']][-1]
            await output(client, response='command-' + renewed['response']['metadata'][
                'jpcall_command'], item='renewed', text=client.brief.opener_ja, played=1)
            await callee(client, 'new', 'はい、どうぞ。', start=5000, stop=5600)
            await turns()
            requests = [e for e in peer.sent if e['type'] == EVENTS['response_create']]
            assert len(requests) == before + 2
            assert requests[-1]['response']['tool_choice']['name'] == 'permission'
        finally:
            await client.stop()
    asyncio.run(run())


def test_transfer_invalidates_remaining_tools_in_the_same_response():
    async def run():
        decisions = []

        async def tool(name, arguments, call_id):
            decisions.append(arguments['decision'])
            if arguments['decision'] == 'transfer':
                client.set_permission(False)

        client, audio, events, meter, peer = bridge(on_tool_call=tool)
        await attach_and_open(client, Media())
        try:
            await client.handle_event({'type': EVENTS['response_created'],
                                       'response': {'id': 'current-tools'}})
            await client.handle_event({'type': EVENTS['response_done'], 'response': {
                'id': 'current-tools', 'status': 'completed', 'output': [
                    {'type': 'function_call', 'status': 'completed', 'name': 'permission',
                     'call_id': decision, 'arguments': json.dumps({'decision': decision})}
                    for decision in ('transfer', 'granted')]}})
            assert decisions == ['transfer'] and not client._permission
        finally:
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('capture', ['unknown', 'old', 'before_notice'])
@pytest.mark.parametrize('event', ['input_transcript', 'input_transcript_failed', 'speech_stopped'])
def test_unknown_or_old_input_cannot_trigger_reply_or_recovery(capture, event, monkeypatch):
    monkeypatch.setattr(realtime, 'ASR_TIMEOUT_S', .01)

    async def run():
        client, audio, events, meter, peer = bridge()
        await attach_and_open(client, Media())
        try:
            if capture == 'old':
                await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'old',
                                           'audio_start_ms': 100})
            client.set_permission(False)
            if capture == 'before_notice':
                await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'old',
                                           'audio_start_ms': 100})
            await client.repeat_notice()
            before = len(peer.sent)
            await client.handle_event({'type': EVENTS[event], 'item_id': 'old',
                                       'transcript': 'はい、どうぞ。', 'audio_end_ms': 200})
            await asyncio.sleep(.03)
            assert not any(e['type'] == EVENTS['response_create'] for e in peer.sent[before:])
            assert client._asr_recovery is None and not client._silenced
        finally:
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('outer_cancel', [False, True])
def test_stalled_close_cannot_retain_content_or_receive_and_watch_tasks(monkeypatch, outer_cancel):
    import gc
    import weakref
    from types import SimpleNamespace

    monkeypatch.setattr(realtime, 'TRANSPORT_CLOSE_TIMEOUT_S', .02)

    async def run():
        close_entered, aborted = asyncio.Event(), []
        client, audio, events, meter, peer = bridge()

        async def stalled_close():
            close_entered.set()
            await asyncio.Event().wait()

        peer.close = stalled_close
        peer._response = SimpleNamespace(close=lambda: aborted.append(True))

        class Owner:
            async def emit(self, event):
                pass

            def context(self):
                return 1

            def speech(self, *args):
                pass

            async def tool(self, *args):
                pass

            async def failed(self):
                pass

        owner = Owner()
        retained = weakref.ref(owner)
        client.events = owner
        client._publication_context = owner.context
        client._on_speech, client._on_callee_response = owner.speech, owner.speech
        client._on_tool_call, client._on_asr_failure = owner.tool, owner.failed
        client._goal = 'synthetic private goal'
        media = Media()
        await attach_and_open(client, media)
        receive = client._task
        watch = asyncio.create_task(client.wait())
        side = client._background(asyncio.Event().wait())
        client._transcripts[('pending', 0)] = 'synthetic private transcript'
        client._partial_transcripts['pending'] = 'synthetic partial'
        client._recent_speech.append((0, 'pending', 'synthetic private speech'))
        del owner
        stopper = asyncio.create_task(client.stop())
        await close_entered.wait()
        # This check runs while close is still stalled, before any close timeout.
        assert aborted == [True]
        assert client.brief is None and client._goal == ''
        assert client.events is None and client.audio_sink is None
        assert client._on_tool_call is None and client._publication_context is None
        assert client._session is None and client._ws is None and client._media is None
        assert not client._transcripts and not client._partial_transcripts and not client._recent_speech
        if outer_cancel:
            stopper.cancel()
        await asyncio.gather(stopper, watch, return_exceptions=True)
        assert receive.done() and watch.done() and side.done()
        assert not client._side_tasks and client._task is None
        gc.collect()
        assert retained() is None
        client.force_cleanup()  # Parent finally and repeated stop remain safe.
        await client.stop()
        await turns()
        assert media.closed if not outer_cancel else not client._media
    asyncio.run(run())
