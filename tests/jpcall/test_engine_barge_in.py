"""Korean interruption fixtures exercise transport/control, never a live voice model."""

import asyncio
import base64
from dataclasses import replace

import pytest
from test_engine_speech_regressions import callee, output, turns
from test_engine_transport import EVENTS, ULAW, Media, attach_and_open, bridge

import fri.jpcall.realtime as realtime
from fri.jpcall.contract import Limits
from fri.jpcall.hold import HoldController


async def inquiry(*, created=True, **kwargs):
    client, audio, events, meter, peer = bridge(publication_context=lambda: 9,
        goal='Ask whether seven o’clock is available; do not book.', **kwargs)
    client.config = replace(client.config, call_language='ko')
    client.brief = replace(client.brief, opener_ja=client.config.language.opener)
    media = Media()
    await attach_and_open(client, media)
    original = peer.send_json
    resource = {}

    async def send(event):
        if event['type'] == EVENTS['response_create'] and not resource:
            peer.sent.append(event)
            resource.update(id='command-2', metadata=event['response']['metadata'], output=[])
            if created:
                peer.feed({'type': EVENTS['response_created'], 'response': resource.copy()})
        elif (event['type'] == EVENTS['response_cancel']
              and event['response_id'] == resource.get('id')):
            peer.sent.append(event)  # Deliberately withhold response.done / cancellation ack.
        else:
            await original(event)

    peer.send_json = send
    generated = await client.respond()
    await turns()
    return client, audio, events, peer, media, resource, generated


def requests(peer):
    return [e['response'] for e in peer.sent if e['type'] == EVENTS['response_create']]


async def acknowledge_cancel(client, resource):
    await client.handle_event({'type': EVENTS['response_done'],
                              'response': resource | {'status': 'cancelled'}})
    await turns()


@pytest.mark.parametrize('before_vad,guard', [
    (True, 'current'), (False, 'current'), (False, 'revoked'),
    (False, 'silenced'), (False, 'unplayed'),
])
def test_delayed_fully_played_transcript_keeps_delivery_evidence(before_vad, guard):
    async def run():
        client, audio, events, peer, media, resource, generated = await inquiry()
        try:
            common = dict(response_id=resource['id'], item_id='heard', content_index=0)
            peer.feed(common | {'type': EVENTS['output_audio'],
                               'delta': base64.b64encode(ULAW).decode()})
            peer.feed(common | {'type': EVENTS['output_audio_done']})
            await turns()
            if guard != 'unplayed':
                for mark in list(client._marks):
                    await client.mark(mark)
                part = client._playout['heard', 0]
                assert part.played_bytes == part.sent_bytes == len(ULAW)
            if guard == 'revoked':
                client.set_permission(False)
            elif guard == 'silenced':
                client.silence()
            transcript = common | {'type': EVENTS['output_transcript'],
                                    'transcript': '일곱 시에 가능할까요?'}
            vad = {'type': EVENTS['speech_started'], 'item_id': 'interrupt', 'audio_start_ms': 100}
            for event in ([transcript, vad] if before_vad else [vad, transcript]):
                peer.feed(event)
            await turns()
            assert client._task and not client._task.done()
            rows = [e for e in events.items if e.item_id == 'heard']
            if guard == 'current':
                assert len(rows) == 1 and rows[0].delivery == 'complete'
                assert rows[0].consent_generation == 9
            else:
                assert not rows
        finally:
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('played', [0, 1, 2])
def test_korean_correction_stops_both_outputs_before_asr_and_continues_same_conversation(played):
    async def run():
        delivered = []
        client, audio, events, peer, media, resource, generated = await inquiry(
            on_callee_response=lambda: delivered.append(True))
        try:
            await output(client, response=resource['id'], text='여섯 시를 확인해 볼게요.',
                         chunks=2, played=played, finished=False)
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'correction',
                                       'audio_start_ms': 100})
            # No transcription, speech_stopped or cancellation acknowledgement has arrived.
            assert audio.cleared == ['fri'] and media.sent[-1] == ('clear',)
            truncation = next(e for e in peer.sent if e['type'] == EVENTS['truncate'])
            assert truncation['audio_end_ms'] == played * 20
            assert any(e['type'] == EVENTS['response_cancel'] for e in peer.sent)
            assert not generated.done() and len(requests(peer)) == 2 and not delivered
            assert not client._marks
            await client.mark('2')  # Cleared marks are never playback evidence.
            frames = len(audio.frames)
            await output(client, response=resource['id'], item='late-old-item',
                         text='여섯 시입니다.', finished=False)
            assert len(audio.frames) == frames
            if played:
                assert next(e for e in events.items if e.speaker == 'fri').delivery == 'interrupted'
            else:
                assert not any(e.speaker == 'fri' for e in events.items)
            await client.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'correction',
                                       'audio_end_ms': 200})
            await turns()
            assert len(requests(peer)) == 2  # VAD stop alone cannot authorize a reply.
            text = '여섯 시가 아니라 일곱 시예요'
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'correction',
                                       'transcript': text})
            await turns()
            assert len(requests(peer)) == 2  # Still waiting for the OLD generation to finish.
            await acknowledge_cancel(client, resource)
            assert await generated == 'cancelled'
            assert len(requests(peer)) == 3
            response = requests(peer)[-1]
            assert 'conversation' not in response and 'instructions' not in response
            assert sum(e['type'] == EVENTS['session_update'] for e in peer.sent) == 1
            assert not any(e['type'] == EVENTS['item_delete'] for e in peer.sent)
            row = next(e for e in events.items if e.item_id == 'correction')
            assert row.text_ja == text and row.delivery == 'complete' and row.consent_generation == 9
            await output(client, response='command-3', item='continued',
                         text='네, 일곱 시를 확인할게요.', played=1)
            assert len(audio.frames) == frames + 1 and delivered == [True]
            assert not any(e['type'] == EVENTS['response_cancel'] and e['response_id'] == 'command-3'
                           for e in peer.sent)
            await client.audio(b'\xfe' * 160)
            assert not any(entry == ('media', b'\xfe' * 160) for entry in media.sent)
        finally:
            await client.stop()
    asyncio.run(run())


def test_early_vad_fences_response_before_its_created_event_or_first_audio():
    async def run():
        client, audio, events, peer, media, resource, generated = await inquiry(created=False)
        try:
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'early',
                                       'audio_start_ms': 0})
            assert media.sent == [('clear',)] and audio.cleared == ['fri']
            await client.handle_event({'type': EVENTS['response_created'], 'response': resource})
            await output(client, response=resource['id'], text='여섯 시를 확인할게요.')
            assert not audio.frames and not any(e.speaker == 'fri' for e in events.items)
            assert len(requests(peer)) == 2
            await callee(client, 'early', '여섯 시가 아니라 일곱 시예요')
            await acknowledge_cancel(client, resource)
            assert len(requests(peer)) == 3 and await generated == 'cancelled'
        finally:
            await client.stop()
    asyncio.run(run())


def test_multiple_current_starts_wait_for_all_asr_and_duplicate_start_is_idempotent():
    async def run():
        client, audio, events, peer, media, resource, generated = await inquiry()
        try:
            await output(client, response=resource['id'], finished=False)
            for item in ('first', 'second', 'second'):
                await client.handle_event({'type': EVENTS['speech_started'], 'item_id': item,
                                           'audio_start_ms': 100})
            assert audio.cleared == ['fri', 'fri']
            assert len([e for e in peer.sent if e['type'] == EVENTS['truncate']]) == 1
            await acknowledge_cancel(client, resource)
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'first',
                                       'transcript': '여섯 시가 아니라 일곱 시예요'})
            await turns()
            assert len(requests(peer)) == 2
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'second',
                                       'transcript': '일곱 시로 확인해 주세요'})
            await turns()
            assert len(requests(peer)) == 3 and not client._speech_pending
            await output(client, response='command-3', item='second-reply', finished=False)
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'third',
                                       'audio_start_ms': 300})
            assert media.sent[-1] == ('clear',) and audio.cleared[-1] == 'fri'
            await callee(client, 'third', '예약하지 말고 시간만 확인해 주세요')
            await turns()
            assert len(requests(peer)) == 4
            assert all('instructions' not in response for response in requests(peer)[2:])
        finally:
            await client.stop()
    asyncio.run(run())


def test_queued_hold_speech_is_suppressed_but_group_answer_data_survives_interruption():
    async def run():
        now = [0]
        client, audio, events, peer, media, resource, generated = await inquiry()

        async def noop(*args):
            pass

        hold = HoldController('request1', Limits(), noop, client.say, client.tool_result, noop,
                              clock=lambda: now[0])
        client._on_speech, client._on_callee_response = hold.callee_speech, hold.callee_response
        try:
            await hold.ask('group-tool', 'Which time?', ())
            await turns()  # Hold speech is queued behind the active inquiry response.
            now[0] = 45
            await hold.tick()
            await turns()
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'new-turn',
                                       'audio_start_ms': 100})
            question_id = hold.question.question_id
            assert not hold.question.checked_in and hold._checkin is None
            await acknowledge_cancel(client, resource)
            assert len(requests(peer)) == 2  # No queued hold line/check-in starts afterward.
            await client.tool_result('group-tool', {'answer': 'seven o’clock'})
            assert any(e['type'] == EVENTS['item_create']
                       and 'seven o' in e['item']['output'] for e in peer.sent)
            assert len(requests(peer)) == 2 and not hold.question.checked_in
            await callee(client, 'new-turn', '일곱 시로 확인해 주세요')
            await turns()
            assert len(requests(peer)) == 3 and not hold.question.checked_in
            response_id = 'command-' + requests(peer)[-1]['metadata']['jpcall_command']
            await output(client, response=response_id, item='delivered-answer', played=1)
            assert hold.question.checked_in and hold.question.question_id == question_id
        finally:
            await hold.close()
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['failed', 'empty', 'missing'])
def test_pending_vad_releases_on_asr_failure_or_deadline_with_bounded_recovery(monkeypatch, failure):
    monkeypatch.setattr(realtime, 'ASR_TIMEOUT_S', .02)

    async def run():
        exhausted = asyncio.Event()

        async def failed():
            exhausted.set()

        client, audio, events, peer, media, resource, generated = await inquiry(on_asr_failure=failed)
        try:
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'lost',
                                       'audio_start_ms': 100})
            await acknowledge_cancel(client, resource)
            await client.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'lost',
                                       'audio_end_ms': 200})
            if failure != 'missing':
                await client.handle_event({'type': EVENTS[
                    'input_transcript_failed' if failure == 'failed' else 'input_transcript'],
                    'item_id': 'lost', 'transcript': ''})
            await asyncio.sleep(.1)  # Five ASR deadlines of silence after the clarification.
            assert not client._speech_pending and not client._silenced and not exhausted.is_set()
            assert len(requests(peer)) == 3  # Opening, interrupted inquiry, one clarification.
        finally:
            await client.stop()
    asyncio.run(run())


def test_prepermission_notice_keeps_existing_vad_behavior_and_stale_start_cannot_interrupt():
    async def run():
        client, audio, events, meter, peer = bridge()
        media = Media()
        await client.start(media)
        await client.open_conversation()
        try:
            await output(client, text=client.brief.opener_ja, finished=False)
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'notice-reply',
                                       'audio_start_ms': 0})
            assert not audio.cleared and not client._speech_pending
            assert not any(e['type'] in (EVENTS['truncate'], EVENTS['response_cancel'])
                           for e in peer.sent)
            client.set_permission(True)
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'notice-reply',
                                       'audio_start_ms': 0})
            assert not audio.cleared and not client._speech_pending
        finally:
            await client.stop()
    asyncio.run(run())


def test_phone_clear_and_truncate_do_not_wait_for_cancel_send_or_acknowledgement():
    async def run():
        client, audio, events, peer, media, resource, generated = await inquiry()
        release = asyncio.Event()
        original = peer.send_json

        async def slow_cancel(event):
            if event['type'] == EVENTS['response_cancel']:
                await release.wait()
            await original(event)

        peer.send_json = slow_cancel
        await output(client, response=resource['id'], chunks=2, played=1, finished=False)
        start = asyncio.create_task(client.handle_event({'type': EVENTS['speech_started'],
            'item_id': 'interrupt', 'audio_start_ms': 100}))
        try:
            await turns()
            assert not start.done()
            assert audio.cleared == ['fri'] and media.sent[-1] == ('clear',)
            assert next(e for e in peer.sent if e['type'] == EVENTS['truncate'])['audio_end_ms'] == 20
            assert len(requests(peer)) == 2
            release.set()
            await start
        finally:
            release.set()
            start.cancel()
            await asyncio.gather(start, return_exceptions=True)
            await client.stop()
    asyncio.run(run())


def test_discord_fence_precedes_waiting_for_an_inflight_phone_write():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        client, audio, events, peer, media, resource, generated = await inquiry()
        original = media.send_audio

        async def slow_write(data):
            entered.set()
            await release.wait()
            await original(data)

        media.send_audio = slow_write
        sending = asyncio.create_task(output(client, response=resource['id'], finished=False))
        await entered.wait()
        start = asyncio.create_task(client.handle_event({'type': EVENTS['speech_started'],
            'item_id': 'interrupt', 'audio_start_ms': 100}))
        try:
            await turns()
            assert audio.cleared == ['fri'] and resource['id'] in client._blocked_responses
            assert not audio.frames and not client._marks
            release.set()
            await asyncio.gather(start, sending)
            assert media.sent[-1] == ('clear',) and not audio.frames
            assert next(e for e in peer.sent if e['type'] == EVENTS['truncate'])['audio_end_ms'] == 0
        finally:
            release.set()
            start.cancel()
            sending.cancel()
            await asyncio.gather(start, sending, return_exceptions=True)
            await client.stop()
    asyncio.run(run())


def test_delayed_asr_consumer_cannot_restart_a_reply_after_permission_epoch_changes():
    async def run():
        client, audio, events, peer, media, resource, generated = await inquiry()
        entered, release = asyncio.Event(), asyncio.Event()
        original = events.emit

        async def slow_emit(event):
            if getattr(event, 'speaker', '') == 'callee':
                entered.set()
                await release.wait()
            await original(event)

        events.emit = slow_emit
        await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'old-input',
                                   'audio_start_ms': 100})
        await acknowledge_cancel(client, resource)
        final = asyncio.create_task(client.handle_event({'type': EVENTS['input_transcript'],
            'item_id': 'old-input', 'transcript': '여섯 시가 아니라 일곱 시예요'}))
        try:
            await entered.wait()
            client.set_permission(False)
            await client.repeat_notice()
            client.set_permission(True)
            before = len(requests(peer))
            release.set()
            await final
            await turns()
            assert len(requests(peer)) == before
            assert events.items[-1].consent_generation == 9  # Saved capture context, not retagged.
            await callee(client, 'fresh-input', '일곱 시로 확인해 주세요')
            await turns()
            assert len(requests(peer)) == before + 1
        finally:
            release.set()
            final.cancel()
            await asyncio.gather(final, return_exceptions=True)
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('late_created', [False, True])
def test_canceled_command_cannot_deliver_late_permission_tools_or_restart_audio(late_created):
    async def run():
        calls = []

        async def tool(name, arguments, call_id):
            calls.append((name, arguments))

        client, audio, events, peer, media, resource, generated = await inquiry(
            created=False, on_tool_call=tool)
        try:
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'pending',
                                       'audio_start_ms': 100})
            # The canceled queued command may surface with a late id; its command identity stays old.
            late = resource | {'id': 'late-id', 'status': 'completed', 'output': [
                {'type': 'function_call', 'status': 'completed', 'name': 'permission',
                 'call_id': 'stale-' + decision, 'arguments': '{"decision":"' + decision + '"}'}
                for decision in ('transfer', 'refused', 'granted')]}
            if late_created:
                await client.handle_event({'type': EVENTS['response_created'], 'response': late})
            await client.handle_event({'type': EVENTS['response_done'], 'response': late})
            await output(client, response='late-id', item='late-audio')
            assert not calls and not audio.frames and len(requests(peer)) == 2
            assert 'late-id' in client._blocked_responses and client._speech_pending == {'pending'}
        finally:
            await client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('decision', ['refused', 'transfer'])
def test_current_control_tool_can_revoke_during_pending_input_without_unlocking_audio(decision):
    async def run():
        calls = []
        client = None

        async def tool(name, arguments, call_id):
            calls.append(arguments['decision'])
            client.set_permission(arguments['decision'] == 'granted')

        client, audio, events, peer, media, resource, generated = await inquiry(on_tool_call=tool)
        try:
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'pending',
                                       'audio_start_ms': 100})
            # Existing synthetic control seam, independent of the command canceled at VAD start.
            control = {'id': 'current-control', 'status': 'completed', 'output': [
                {'type': 'function_call', 'status': 'completed', 'name': 'permission',
                 'call_id': 'current-' + value, 'arguments': '{"decision":"' + value + '"}'}
                for value in (decision, 'granted')]}
            await client.handle_event({'type': EVENTS['response_created'], 'response': control})
            await output(client, response=control['id'], item='control-audio')
            assert not audio.frames  # Tool delivery does not authorize concurrent speech.
            await client.handle_event({'type': EVENTS['response_done'], 'response': control})
            assert calls == [decision] and not client._permission
            await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'pending',
                                       'transcript': '여섯 시가 아니라 일곱 시예요'})
            await turns()
            assert len(requests(peer)) == 2
        finally:
            await client.stop()
    asyncio.run(run())


def test_known_command_from_old_permission_epoch_cannot_deliver_late_grant():
    async def run():
        calls = []

        async def tool(name, arguments, call_id):
            calls.append(arguments)

        client, audio, events, peer, media, resource, generated = await inquiry(
            on_tool_call=tool)
        try:
            client.set_permission(True)  # A new source epoch supersedes the in-flight command.
            late = resource | {'status': 'completed', 'output': [{
                'type': 'function_call', 'status': 'completed', 'name': 'permission',
                'call_id': 'old-epoch', 'arguments': '{"decision":"granted"}'}]}
            await client.handle_event({'type': EVENTS['response_done'], 'response': late})
            assert not calls and resource['id'] in client._blocked_responses
        finally:
            await client.stop()
    asyncio.run(run())
