"""Ordinary close versus withdrawal, owned creates, create evidence, the result and the deadline."""

import asyncio
import base64
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_engine_config import environment
from test_engine_flow import (
    SID,
    CallAudio,
    CallEvents,
    Provider,
    answer_phone,
    dial,
    make_engine,
    request,
    settled,
)
from test_engine_permission import DelayedPhone
from test_engine_transport import ULAW

import fri.jpcall.engine as engine_module
import fri.jpcall.realtime as realtime
from fri.jpcall.config import JpcallConfig
from fri.jpcall.contract import (
    CallFinished,
    CallRejected,
    CallState,
    Limits,
    QuestionAsked,
    QuestionClosed,
    StatusChanged,
    Translation,
    Utterance,
)
from fri.jpcall.realtime import EVENTS
from fri.jpcall.recovery import AttemptJournal


class Gated(Provider):
    """The flow provider, with held translations/summaries and selectable create failures."""

    def __init__(self, *, create=None, status='in-progress'):
        super().__init__()
        self.translation_gate = self.summary_gate = None
        self.summary_delay = 0
        self.summaries = []
        self.create, self.status = create, status

    async def handle(self, request):
        if request.url.host == 'api.openai.com':
            instructions = json.loads(request.content)['input'][0]['content']
            if instructions.startswith('Translate') and self.translation_gate is not None:
                await self.translation_gate.wait()
            elif instructions.startswith('The call transcript'):
                self.summaries.append(json.loads(request.content)['input'][1]['content'])
                if self.summary_gate is not None:
                    await self.summary_gate.wait()
                await asyncio.sleep(self.summary_delay)
        elif request.method == 'GET' and self.status:
            self.requests.append(request)
            self.order.append('status')
            return httpx.Response(200, json={'sid': SID, 'status': self.status})
        elif self.create and b'To=' in request.content:
            self.requests.append(request)
            self.order.append('create')
            if self.create == 'connect':
                raise httpx.ConnectError('synthetic: no connection', request=request)
            return httpx.Response(self.create, json={'code': 21215, 'status': self.create,
                                                     'message': 'synthetic rejection'})
        return await super().handle(request)


async def callee_line(call, item, text, start_ms=9000, *, transcript=True):
    rt = call._rt
    await rt.handle_event({'type': EVENTS['speech_started'], 'item_id': item,
                           'audio_start_ms': start_ms})
    await rt.handle_event({'type': EVENTS['speech_stopped'], 'item_id': item,
                           'audio_end_ms': start_ms + 900})
    if transcript:
        await rt.handle_event({'type': EVENTS['input_transcript'], 'item_id': item,
                               'transcript': text, 'usage': None})


def kinds(events, item=None):
    return [type(e).__name__ for e in events.items
            if item is None or getattr(e, 'item_id', item) == item]


def farewells(engine, peer, start=0):
    return sum(engine.config.language.farewell in r.get('instructions', '')
               for r in peer.responses[start:])


# ---- 03: an ordinary close keeps authorized evidence; a withdrawal still revokes it ----------

@pytest.mark.parametrize('ending', ['callee_hangup', 'requester_stop'])
def test_ordinary_close_keeps_the_final_line_and_its_late_translation(tmp_path, monkeypatch, ending):
    async def run():
        provider = Gated()
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            provider.translation_gate = asyncio.Event()
            await callee_line(call, 'final', '七時なら二名様で空いています。')
            await settled(lambda: 'Utterance' in kinds(events, 'final'))
            assert call.publication_allowed and call._rt._input_context['final'] is not None
            if ending == 'callee_hangup':
                await call.status({'CallSid': SID, 'CallStatus': 'completed', 'SequenceNumber': '9'})
                stopping = None
            else:
                stopping = asyncio.create_task(call.hang_up())
            await settled(lambda: call._ending)
            # Live relay and new capture stop at once; consent for captured text does not.
            assert not engine._server._status[call._routes.status_token].relay_allowed()
            assert call._rt._publication_context() is None
            consent_kept = call.publication_allowed
            await asyncio.sleep(.05)
            provider.translation_gate.set()
            await asyncio.wait_for(call._finished.wait(), 5)
            if stopping is not None:
                await stopping
            order = kinds(events)
            assert 'Translation' in kinds(events, 'final')
            assert order.index('Translation', order.index('Utterance')) < order.index('CallFinished')
            assert any('七時なら二名様で空いています。' in summary for summary in provider.summaries)
            assert farewells(engine, peer) == 0  # Neither a hang-up nor Stop says goodbye.
            assert consent_kept  # Closing is not a withdrawal.
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('ending', ['callee_hangup', 'requester_stop'])
def test_final_asr_after_the_call_ends_is_published_and_summarized(tmp_path, monkeypatch, ending):
    async def run():
        provider = Gated()
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            rt = call._rt
            await callee_line(call, 'last', '', transcript=False)  # Speech ended; ASR pending.
            if ending == 'callee_hangup':
                await call.status({'CallSid': SID, 'CallStatus': 'completed', 'SequenceNumber': '9'})
            else:
                stopping = asyncio.create_task(call.hang_up())
            await asyncio.sleep(.2)
            await rt.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'last',
                                   'transcript': '八時なら空いています。', 'usage': None})
            await asyncio.wait_for(call._finished.wait(), 5)
            if ending == 'requester_stop':
                await stopping
            line = next(e for e in events.items if isinstance(e, Utterance) and e.item_id == 'last')
            assert line.delivery == 'complete' and 'Translation' in kinds(events, 'last')
            assert any('八時なら空いています。' in summary for summary in provider.summaries)
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


class ClosingPhone(DelayedPhone):
    def __init__(self, call, order):
        super().__init__(call)
        self.order = order

    async def clear(self):
        self.order.append('phone_clear')


async def answered_with(call, media):
    await call.status({'CallSid': SID, 'CallStatus': 'in-progress', 'SequenceNumber': '1'})
    await call.start(media)
    await call.status({'CallSid': SID, 'AnsweredBy': 'human'})
    await call._rt.handle_event({'type': EVENTS['speech_started'], 'item_id': 'hello',
                                 'audio_start_ms': 0})
    await call._rt.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'hello'})
    await settled(lambda: call._rt._conversation_open)
    await media.finish_playback()
    await call._handle_tool('permission', {'decision': 'granted'}, 'fixture-permission')
    await settled(lambda: call._rt._idle.is_set() and bool(media.marks))
    await media.finish_playback()


@pytest.mark.parametrize('spoken', [True, False])
def test_end_call_gets_exactly_one_goodbye_and_its_spoken_line_is_not_cleared(
        tmp_path, monkeypatch, spoken):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = ClosingPhone(call, provider.order)
            await answered_with(call, media)
            original = peer.send_json

            async def model(event):  # The next ordinary reply says goodbye and calls end_call.
                if event['type'] != EVENTS['response_create'] or 'instructions' in event['response']:
                    return await original(event)
                peer.sent.append(event)
                peer.send_json = original
                resource = {'id': 'bye', 'metadata': event['response']['metadata'],
                            'status': 'completed', 'usage': None, 'output': [{
                                'type': 'function_call', 'status': 'completed', 'name': 'end_call',
                                'call_id': 'end1',
                                'arguments': json.dumps({'outcome_en': 'Seven works.'})}]}
                peer.feed({'type': EVENTS['response_created'], 'response': resource})
                if spoken:
                    for _ in range(3):
                        peer.feed({'type': EVENTS['output_audio'], 'response_id': 'bye',
                                   'item_id': 'bye-item', 'content_index': 0,
                                   'delta': base64.b64encode(ULAW).decode()})
                    peer.feed({'type': EVENTS['output_audio_done'], 'response_id': 'bye',
                               'item_id': 'bye-item', 'content_index': 0})
                    peer.feed({'type': EVENTS['output_transcript'], 'response_id': 'bye',
                               'item_id': 'bye-item', 'content_index': 0,
                               'transcript': 'では七時で。ありがとうございました。'})
                peer.feed({'type': EVENTS['response_done'], 'response': resource})

            peer.send_json = model
            before = len(peer.responses)
            await callee_line(call, 'ok', 'はい、七時で大丈夫です。')  # Its VAD start may clear.
            clears = provider.order.count('phone_clear')
            await settled(lambda: call._ending)
            await asyncio.sleep(.05)
            assert provider.order.count('phone_clear') == clears and 'hangup' not in provider.order
            assert not any(e['type'] == EVENTS['truncate'] for e in peer.sent)
            for _ in range(5):  # The phone finishes playing whatever goodbye is queued.
                await media.finish_playback()
                await asyncio.sleep(.01)
            await asyncio.wait_for(call._finished.wait(), 5)
            assert farewells(engine, peer, before) == (0 if spoken else 1)
            # Nothing cleared the closing audio before the hang-up; only the final stop clears.
            assert provider.order[:provider.order.index('hangup')].count('phone_clear') == clears
            if spoken:
                line = next(e for e in events.items if getattr(e, 'item_id', '') == 'bye-item')
                assert line.delivery == 'complete'  # Fully played, so published as evidence.
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_withdrawal_during_close_still_revokes_queued_text_and_the_summary(tmp_path, monkeypatch):
    async def run():
        provider = Gated()
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            provider.translation_gate, provider.summary_gate = asyncio.Event(), asyncio.Event()
            await callee_line(call, 'private', 'やっぱり共有しないでください。')
            await call.status({'CallSid': SID, 'CallStatus': 'completed', 'SequenceNumber': '9'})
            await settled(lambda: bool(provider.summaries))
            await call._tool('permission', {'decision': 'refused'}, 'withdraw')
            assert not call.publication_allowed and not call._transcript
            provider.translation_gate.set()
            provider.summary_gate.set()
            await asyncio.wait_for(call._finished.wait(), 5)
            assert 'Translation' not in kinds(events, 'private')
            finish = next(e for e in events.items if isinstance(e, CallFinished))
            assert 'Lunch' not in finish.outcome_en and 'unverified' not in finish.outcome_en
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def line(item, text, at, generation=1):
    return Utterance('r', item, 'callee', text, at, consent_generation=generation)


@pytest.mark.parametrize('ending', ['ordinary', 'stop', 'withdrawn'])
def test_discord_final_page_keeps_throttled_rows_unless_consent_was_withdrawn(ending):
    from test_discord_command import Setup

    async def run():
        setup = Setup()
        call = await setup.start()  # The production pager and its consent predicate.
        call.pager.interval = .3
        session, rid = setup.session, call.request.request_id

        async def emit(event):
            await call.emit(replace(event, request_id=rid))

        await emit(line('a', 'はい、さくら食堂です。', 1))
        await call.flush_events()
        await emit(Translation('r', 'a', 'Sakura Shokudo.', consent_generation=1))
        await emit(line('b', '七時なら空いています。', 2))  # Waits in the edit throttle.
        await emit(line('c', 'では失礼します。', 3))
        await asyncio.sleep(.05)
        withdrawn = ending == 'withdrawn'
        if withdrawn:
            session.publication_allowed, session.consent_generation = False, 2
        elif ending == 'stop':  # As HangupView/stop() leave the call before the engine's events.
            call.phase, call._stop_reason = 'ended', 'requested'
        await emit(Translation('r', 'b', 'Seven is available.', consent_generation=1))
        await emit(StatusChanged('r', CallState.ENDED))
        await emit(CallFinished('r', 'done', 3.0, termination_confirmed=True))
        await call.flush_events()
        await emit(Translation('r', 'c', 'Goodbye.', consent_generation=1))  # After the end.
        await call.flush_events()
        final = '\n'.join(m.content or '' for m in setup.messages if 'さくら' in (m.content or ''))
        assert ('七時なら空いています。' in final) is (not withdrawn)
        assert ('Seven is available.' in final) is (not withdrawn)
        if not withdrawn:  # No English ever came for c: unavailable, never pending forever.
            assert 'では失礼します。\n↳ translation unavailable' in final
            assert 'Goodbye.' not in final and 'pending' not in final
    asyncio.run(run())


class RecordingPhone(DelayedPhone):
    """Holds playback marks until released and records phone writes."""

    def __init__(self, call):
        super().__init__(call)
        self.log = []

    async def send_audio(self, ulaw):
        self.log.append('audio')

    async def clear(self):
        self.log.append('clear')


def held_responses(peer, *, hold_instructions=None):
    """The next matching response_create starts speaking one chunk and stays in progress."""
    original, started = peer.send_json, []

    async def send_json(event):
        if (event['type'] != EVENTS['response_create']
                or (hold_instructions is None) != ('instructions' not in event['response'])
                or (hold_instructions and hold_instructions not in event['response']['instructions'])):
            return await original(event)
        peer.sent.append(event)
        peer.responses.append(event['response'])
        response_id = 'held-' + event['response']['metadata']['jpcall_command']
        started.append(response_id)
        peer.feed({'type': EVENTS['response_created'], 'response': {
            'id': response_id, 'metadata': event['response']['metadata'], 'status': 'in_progress',
            'output': []}})
        chunk(peer, response_id, response_id + '-item')
    peer.send_json = send_json
    return started


def chunk(peer, response_id, item):
    peer.feed({'type': EVENTS['output_audio'], 'response_id': response_id, 'item_id': item,
               'content_index': 0, 'delta': base64.b64encode(ULAW).decode()})


def test_requester_stop_fences_active_speech_at_once_and_keeps_played_evidence(
        tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            rt, media = call._rt, RecordingPhone(call)
            rt._media = media
            started = held_responses(peer)
            await rt.respond()
            await settled(lambda: bool(started) and len(media.marks) == 1)
            response, item = started[0], started[0] + '-item'
            chunk(peer, response, item)
            await settled(lambda: len(media.marks) == 2)
            await call.mark(media.marks.pop(0))  # The phone has played the first 20 ms only.
            gate, hangup = asyncio.Event(), engine._twilio.hang_up

            async def delayed_hangup(sid):
                await gate.wait()
                return await hangup(sid)
            engine._twilio.hang_up = delayed_hangup
            pushed = len(audio.frames)
            stop = asyncio.create_task(call.hang_up())
            await asyncio.sleep(0)
            assert call._ending and not rt._output_allowed({'response_id': response})
            sent = media.log.count('audio')
            chunk(peer, response, item)  # The model keeps generating after Stop.
            await settled(lambda: 'clear' in media.log)  # Phone cleared before the hang-up returns.
            assert media.log.count('audio') == sent and len(audio.frames) == pushed
            peer.feed({'type': EVENTS['output_transcript'], 'response_id': response, 'item_id': item,
                       'content_index': 0, 'transcript': '七時のご予約は'})
            await settled(lambda: any(getattr(e, 'item_id', '') == item for e in events.items))
            gate.set()
            await asyncio.wait_for(stop, 5)
            line = next(e for e in events.items if getattr(e, 'item_id', '') == item)
            assert line.delivery == 'interrupted' and line.text_ja == '七時のご予約は'
            truncate = next(e for e in peer.sent if e['type'] == EVENTS['truncate'])
            assert truncate['item_id'] == item and truncate['audio_end_ms'] == 20
            assert farewells(engine, peer) == 0 and provider.hangups == 1
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('speech', ['farewell', 'queued_hold'])
def test_requester_stop_during_the_goodbye_or_with_queued_hold_speech(tmp_path, monkeypatch,
                                                                      speech):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = RecordingPhone(call)
            await answered_with(call, media)
            language = engine.config.language
            if speech == 'farewell':
                started = held_responses(peer, hold_instructions=language.farewell)
                peer.tool('end_call', {'outcome_en': 'Done.'}, 'end1')
                await settled(lambda: bool(started) and bool(media.marks))
                response = started[0]
            else:  # Fri is still speaking, so the hold line waits in the queue.
                started = held_responses(peer)
                await call._rt.respond()
                await settled(lambda: bool(started) and bool(media.marks))
                response = started[0]
                peer.tool('ask_group', {'question_en': 'Ok?', 'question_original': 'q',
                                        'options': []})
                await settled(lambda: any(isinstance(e, QuestionAsked) for e in events.items))
            before = len(peer.responses)
            sent = media.log.count('audio')
            gate, hangup = asyncio.Event(), engine._twilio.hang_up

            async def delayed_hangup(sid):
                await gate.wait()
                return await hangup(sid)
            engine._twilio.hang_up = delayed_hangup
            stop = asyncio.create_task(call.hang_up())
            await asyncio.sleep(0)  # One step: the fence precedes every await of the Stop.
            assert not call._rt._output_allowed({'response_id': response})
            chunk(peer, response, response + '-item')
            await settled(lambda: 'clear' in media.log)  # Before the hang-up request returns.
            assert media.log.count('audio') == sent and not stop.done()
            gate.set()
            await asyncio.wait_for(stop, 5)
            assert not any(language.hold in r.get('instructions', '') or
                           language.farewell in r.get('instructions', '')
                           for r in peer.responses[before:])
            assert call._finished.is_set() and provider.hangups == 1
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('when', ['during_goodbye', 'while_translations_settle'])
def test_withdrawal_after_the_summary_completed_never_reaches_the_report(tmp_path, monkeypatch,
                                                                        when):
    async def run():
        provider = Gated()
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = RecordingPhone(call)
            await answered_with(call, media)
            if when == 'while_translations_settle':
                provider.translation_gate = asyncio.Event()
            await callee_line(call, 'private', '七時なら二名様で空いています。')
            await settled(lambda: 'Utterance' in kinds(events, 'private'))
            if when == 'during_goodbye':  # Fri is mid-reply when the deadline closing begins.
                started = held_responses(peer)
                await call._rt.respond()
                await settled(lambda: bool(started))
                metadata = peer.responses[-1]['metadata']
                clock.advance(600 - 8)
            else:
                peer.tool('end_call', {'outcome_en': 'Seven works.'}, 'end1')
            await settled(lambda: call._ending and bool(provider.summaries))
            await asyncio.sleep(.05)  # The summary has completed under the old consent.
            assert not call._finished.is_set()
            if when == 'during_goodbye':  # That reply, created before closing, reports a refusal.
                peer.feed({'type': EVENTS['response_done'], 'response': {
                    'id': started[0], 'metadata': metadata, 'status': 'completed', 'usage': None,
                    'output': [{'type': 'function_call', 'status': 'completed',
                                'name': 'permission', 'call_id': 'withdraw',
                                'arguments': json.dumps({'decision': 'refused'})}]}})
                await settled(lambda: not call.publication_allowed)
            else:
                await call._tool('permission', {'decision': 'refused'}, 'withdraw')
                provider.translation_gate.set()
            await media.finish_playback()
            await asyncio.wait_for(call._finished.wait(), 5)
            finish = next(e for e in events.items if isinstance(e, CallFinished))
            assert 'Lunch' not in finish.outcome_en and 'Seven' not in finish.outcome_en
            assert finish.consent_generation is None and not call._transcript
            if when == 'while_translations_settle':  # Queued English is revoked too.
                assert not any(isinstance(e, Translation) and e.item_id == 'private'
                               for e in events.items)
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('withdrawn', ['never', 'after_admission', 'before_retry', 'generic'])
def test_final_report_rechecks_consent_at_the_actual_send(withdrawn):
    from test_discord_command import Setup

    from fri.jpcall.discord_cost import WITHHELD

    async def run():
        setup = Setup()
        call = await setup.start()
        session, rid = setup.session, call.request.request_id
        sent, failed = [], []
        original = setup.channel.send.side_effect

        def withdraw():
            session.publication_allowed, session.consent_generation = False, 2

        async def send(content, **kwargs):
            if withdrawn == 'before_retry' and content and 'call ended' in content and not failed:
                failed.append(True)
                withdraw()  # Consent changes while the first attempt fails.
                raise RuntimeError('synthetic transient send failure')
            body = kwargs.get('file')
            sent.append((content or '') + (body.fp.getvalue().decode() if body else ''))
            return await original(content, **kwargs)
        setup.channel.send.side_effect = send
        generation = None if withdrawn == 'generic' else 1
        outcome = ('The business line was busy.' if withdrawn == 'generic'
                   else 'Lunch is available at noon for two.')
        await call.emit(CallFinished(rid, outcome, 61.0, termination_confirmed=True,
                                     consent_generation=generation))
        if withdrawn in ('after_admission', 'generic'):
            withdraw()  # Admitted to the display queue, not yet sent.
        await call.flush_events()
        await call.cost.flush()
        report = '\n'.join(sent)
        edits = '\n'.join(str(m.edits) for m in setup.messages)
        if withdrawn in ('never', 'generic'):
            assert outcome.lower()[:12] in report.lower()
        else:
            assert 'Lunch' not in report and 'Lunch' not in edits and WITHHELD in report
            assert call.cost.finished.outcome_en == WITHHELD  # Not retained for later edits.
    asyncio.run(run())


# ---- 04: Stop during an in-flight create keeps owning it ------------------------------------

def test_stop_during_create_captures_a_late_sid_and_hangs_up_once(tmp_path, monkeypatch):
    async def run():
        provider = Provider()
        provider.create_gate = asyncio.Event()
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            req = request()
            brief = await engine.plan(req)
            audio, events = CallAudio(provider.order), CallEvents(provider.order)
            call = await engine.start(brief, req, audio, events)
            await settled(lambda: 'create' in provider.order)
            stop = asyncio.create_task(call.hang_up())
            await asyncio.sleep(.5)  # The create is accepted well after the old 0.1 s window.
            provider.create_gate.set()
            await asyncio.wait_for(stop, 5)
            assert call._sid == SID and provider.hangups == 1
            assert engine.recovery_status()['admission'] == 'ready'
            assert next(e for e in events.items if isinstance(e, CallFinished)).termination_confirmed
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_create_beyond_the_cleanup_bound_stays_owned_and_a_late_callback_ends_it(
        tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, 'CLEANUP_S', .3, raising=False)

    async def run():
        provider = Gated(status='ringing')
        provider.create_gate = asyncio.Event()  # Never answered within the bound.
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            req = request()
            brief = await engine.plan(req)
            call = await engine.start(brief, req, CallAudio(provider.order), CallEvents(provider.order))
            await settled(lambda: 'create' in provider.order)
            token = call._routes.status_token
            await asyncio.wait_for(call.hang_up(), 5)
            status = engine.recovery_status()
            assert status['admission'] == 'blocked' and not status['sid_known']
            assert provider.hangups == 0  # Durable ambiguity; nothing is redialed.
            assert engine._server.recovery_status(token, {'CallSid': SID, 'CallStatus': 'ringing'})
            await asyncio.wait_for(engine._reconciling, 5)
            assert provider.hangups == 1 and engine.recovery_status()['admission'] == 'ready'
            assert engine._server.recovery_status(token, {'CallSid': SID, 'CallStatus': 'completed'})
            await asyncio.sleep(.05)
            assert provider.hangups == 1 and provider.order.count('create') == 1  # No redial.
        finally:
            provider.create_gate.set()
            await engine.close()
            await http.aclose()
    asyncio.run(run())


# ---- 05: only a provably unsent or unauthenticated create counts as never created -----------

@pytest.mark.parametrize('create,ready', [('connect', True), (401, True), (400, False),
                                          (403, False), (429, False), (500, False)])
def test_create_rejection_evidence_and_restart_admission(tmp_path, monkeypatch, create, ready):
    async def run():
        provider = Gated(create=create, status=None)
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            req = request()
            brief = await engine.plan(req)
            events = CallEvents(provider.order)
            call = await engine.start(brief, req, CallAudio(provider.order), events)
            await asyncio.wait_for(call._finished.wait(), 5)
            status = engine.recovery_status()
            assert (status['admission'] == 'ready') is ready
            assert (status['evidence'] == 'not_created') is ready
            assert (await engine.reconcile())['admission'] == status['admission']
            assert provider.hangups == 0 and provider.order.count('create') == 1
            final = next(e for e in events.items if isinstance(e, StatusChanged)
                         and e.state.value == 'failed')
            assert ('unconfirmed' in final.detail) is (not ready)
        finally:
            await engine.close()
            await http.aclose()
        restarted, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            if ready:
                await restarted.plan(request(request_id='next'))
            else:
                with pytest.raises(CallRejected, match='reconciliation'):
                    await restarted.plan(request(request_id='next'))
        finally:
            await restarted.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('phase,ready', [('reserved', True), ('creating', False)])
def test_reconcile_closes_only_a_provably_pre_create_attempt(tmp_path, monkeypatch, phase, ready):
    async def run():
        directory = tmp_path / 'jpcall'
        AttemptJournal.initialize(directory)
        journal = AttemptJournal(directory)
        journal.open()
        attempt = journal.reserve('s' * 43)
        if phase == 'creating':
            journal.update(attempt, phase='creating')
        journal.close()  # As if the process died here.
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            assert engine.recovery_status()['admission'] == 'blocked'
            status = await engine.reconcile()
            assert (status['admission'] == 'ready') is ready and not provider.requests
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_stopped_runtime_reconcile_refuses_while_running_and_ends_a_known_call(
        tmp_path, monkeypatch):
    async def run():
        provider = Gated(status='in-progress')
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        config = engine.config
        engine._twilio.hang_up = AsyncMock(side_effect=engine_module.TwilioError('synthetic'))
        call, brief, audio, events = await dial(engine, provider)
        await call.hang_up()
        assert engine.recovery_status()['sid_known'] and engine.recovery_status()['phase'] == 'uncertain'
        with pytest.raises(CallRejected):  # The running runtime owns the journal lock.
            await engine_module.reconcile_stopped(config, http_client=http)
        await engine.close()
        try:
            hangups = provider.hangups
            status = await engine_module.reconcile_stopped(config, http_client=http)
            assert status['admission'] == 'ready' and status['evidence'] == 'completed'
            assert provider.hangups == hangups + 1 and provider.order[-2:] == ['status', 'hangup']
            assert set(status) == {'admission', 'attempt', 'sid_known', 'phase', 'evidence',
                                   'started_at'} and SID not in json.dumps(status)
        finally:
            await http.aclose()
    asyncio.run(run())


def test_runbook_command_closes_a_reserved_attempt_left_by_a_crash(tmp_path, monkeypatch, capsys):
    import runpy
    import sys
    directory = tmp_path / 'jpcall'
    AttemptJournal.initialize(directory)
    journal = AttemptJournal(directory)
    journal.open()
    journal.reserve('s' * 43)  # The process died before 'creating': no request was ever sent.
    journal.close()
    for name, value in (environment() | {'FRI_DATA': str(tmp_path)}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, 'argv', ['python -m fri.jpcall.engine'])
    with pytest.warns(RuntimeWarning, match='found in sys.modules'):
        runpy.run_module('fri.jpcall.engine', run_name='__main__')
    status = json.loads(capsys.readouterr().out)
    assert status['admission'] == 'ready' and status['evidence'] == 'not_created'
    assert 'synthetic' not in json.dumps(status)  # Status only: no credential or identifier.


# ---- 09: noise and expected hold silence never end a call ------------------------------------

def test_noise_during_a_group_hold_does_not_end_the_call(tmp_path, monkeypatch):
    monkeypatch.setattr(realtime, 'ASR_TIMEOUT_S', .05)

    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            peer.tool('ask_group', {'question_en': 'Is 7pm ok?', 'question_original': '七時?',
                                    'options': ['yes', 'no']})
            await settled(lambda: any(isinstance(e, QuestionAsked) for e in events.items))
            await callee_line(call, 'blip', '')  # VAD start whose transcript is empty.
            await asyncio.sleep(.3)  # Six ASR deadlines of the callee waiting quietly.
            assert not call._ending and call._hold.question is not None
            clock.advance(60)
            await call._hold.tick()  # The hold deadline still ends it, politely.
            await asyncio.wait_for(call._finished.wait(), 5)
            assert 'did not answer in time' in call._detail and farewells(engine, peer) == 1
            assert any(isinstance(e, QuestionClosed) and e.reason == 'expired'
                       for e in events.items)
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_prepermission_asr_recovery_repeats_the_notice_once_without_waiting_on_playback(
        monkeypatch):
    from test_engine_speech_regressions import turns
    from test_engine_transport import Media, bridge
    monkeypatch.setattr(realtime, 'ASR_TIMEOUT_S', .05)

    async def run():
        exhausted = asyncio.Event()

        async def failed():
            exhausted.set()

        client, audio, events, meter, peer = bridge(on_asr_failure=failed)
        await client.start(Media())
        await client.open_conversation()
        notices = sum(e['type'] == EVENTS['response_create'] for e in peer.sent)
        original = peer.send_json

        async def long_notice(event):  # A long repeated notice keeps playing past 5 ASR timeouts.
            await original(event)
            if event['type'] == EVENTS['response_create']:
                peer.feed({'type': EVENTS['output_audio'], 'item_id': 'repeat',
                           'response_id': 'command-' + event['response']['metadata'][
                               'jpcall_command'], 'content_index': 0, 'delta': base64.b64encode(ULAW).decode()})
        peer.send_json = long_notice
        try:
            for item in ('lost1', 'lost2'):
                await client.handle_event({'type': EVENTS['speech_started'], 'item_id': item,
                                           'audio_start_ms': 100})
                await client.handle_event({'type': EVENTS['input_transcript'], 'item_id': item,
                                           'transcript': ''})
                await turns()
                await asyncio.sleep(.3)
                assert not exhausted.is_set() and not client._silenced
            assert sum(e['type'] == EVENTS['response_create'] for e in peer.sent) == notices + 1
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'out',
                                       'audio_start_ms': 100})
            await client.handle_event({'type': EVENTS['input_transcript_failed'], 'item_id': 'out'})
            await client.handle_event({'type': EVENTS['speech_started'], 'item_id': 'out2',
                                       'audio_start_ms': 100})
            await client.handle_event({'type': EVENTS['input_transcript_failed'], 'item_id': 'out2'})
            await asyncio.wait_for(exhausted.wait(), 1)  # A repeated outage still ends it.
        finally:
            await client.stop()
    asyncio.run(run())


# ---- 10: the result has its own bound; a missing summary never claims there were no facts ---

def test_a_summary_slightly_over_two_seconds_is_used(tmp_path, monkeypatch):
    async def run():
        provider = Gated()
        provider.summary_delay = 2.3
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            peer.tool('end_call', {'outcome_en': 'Lunch at noon.'}, 'end1')
            started = time.monotonic()
            await asyncio.wait_for(call._finished.wait(), 15)
            finish = next(e for e in events.items if isinstance(e, CallFinished))
            assert finish.outcome_en == 'Lunch is available at noon. No reservation was confirmed.'
            assert time.monotonic() - started < getattr(engine_module, 'CLEANUP_S', 8) + 2
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_a_summary_past_its_bound_is_unavailable_and_the_end_hint_stays_unverified(
        tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module, 'CLEANUP_S', 1.0, raising=False)

    async def run():
        provider = Gated()
        provider.summary_delay = 3
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, provider)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            peer.tool('end_call', {'outcome_en': 'Lunch at noon.'}, 'end1')
            started = time.monotonic()
            await asyncio.wait_for(call._finished.wait(), 10)
            assert time.monotonic() - started < 2.5  # Cleanup bound plus the final report.
            finish = next(e for e in events.items if isinstance(e, CallFinished))
            assert finish.outcome_en == ('Summary unavailable; see the transcript. '
                                         'Fri’s unverified note: Lunch at noon.')
            assert 'without a confirmed outcome' not in finish.outcome_en
            assert not engine._sessions and not call._transcript
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


# ---- 11: one attempt deadline; one goodbye; Stop is immediate --------------------------------

def test_one_capped_attempt_deadline_for_config_preview_and_phone():
    assert Limits().attempt_s == 600 and Limits(max_call_s=900).attempt_s == 600
    assert Limits(max_call_s=120).attempt_s == 120
    config = JpcallConfig.from_env(environment() | {'JPCALL_MAX_CALL_S': '900'})
    assert not config.enabled and any('JPCALL_MAX_CALL_S' in r for r in config.disabled_reasons)
    config = JpcallConfig.from_env(environment())
    assert config.enabled and config.limits.attempt_s == 600

    async def run():
        from fri.jpcall.twilio import TwilioClient
        forms = []

        def handler(request):
            from urllib.parse import parse_qs
            forms.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={'sid': SID})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            programmatic = JpcallConfig.from_env(environment())
            programmatic.limits.max_call_s = 900
            await TwilioClient(programmatic, http).create_call('+81312345678', 'm' * 43, 's' * 43)
        assert forms[0]['TimeLimit'] == ['600']
    asyncio.run(run())


def test_brief_preview_shows_the_effective_attempt_deadline():
    from test_discord_command import Setup

    async def run():
        setup = Setup()
        setup.calls.limits = Limits(allowed_user_ids=frozenset({10}), max_call_s=900)
        call, interaction = await setup.plan()
        brief = call.brief_message.kwargs['embed'].description
        assert 'limits · 600s call' in brief and '900' not in brief
    asyncio.run(run())


def test_attempt_deadline_closes_early_enough_for_one_goodbye(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            before = len(peer.responses)
            clock.advance(600 - 8 - 1)  # Default 600 s attempt; closing starts 8 s early.
            await asyncio.sleep(.05)
            assert not call._ending
            clock.advance(1)
            await settled(lambda: call._ending)
            await asyncio.wait_for(call._finished.wait(), 5)
            assert call._detail == 'The call reached its time limit.'
            assert farewells(engine, peer, before) == 1 and provider.hangups == 1
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('ending,goodbye', [('refusal', 1), ('hold_expiry', 1), ('stop', 0)])
def test_refusal_and_expiry_say_one_goodbye_and_stop_says_none(tmp_path, monkeypatch, ending,
                                                              goodbye):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            before = len(peer.responses)
            if ending == 'refusal':
                await call._tool('permission', {'decision': 'refused'}, 'refuse')
            elif ending == 'hold_expiry':
                peer.tool('ask_group', {'question_en': 'Ok?', 'question_original': 'q',
                                        'options': []})
                await settled(lambda: any(isinstance(e, QuestionAsked) for e in events.items))
                await settled(lambda: call._rt._idle.is_set())
                before = len(peer.responses)
                clock.advance(60)
                await call._hold.tick()
            else:
                await call.hang_up()
            await asyncio.wait_for(call._finished.wait(), 5)
            assert farewells(engine, peer, before) == goodbye and provider.hangups == 1
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())
