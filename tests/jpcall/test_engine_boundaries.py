import asyncio
import gc
import json
import weakref
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_engine_flow import SID, answer_phone, dial, make_engine, request, settled

from fri.jpcall.contract import CallFinished, CallRejected, CallState, StatusChanged, Utterance
from fri.jpcall.realtime import EVENTS
from fri.jpcall.twilio import TwilioError


def test_direct_brief_preserves_approved_facts_without_paid_paraphrase(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            req = request(goal='Ask about 7 November at 18:30, four people; no fee acceptance.',
                          share=('party of four',), never=('do not book',))
            brief = await engine.plan(req)
            assert brief.summary_en == req.goal
            assert brief.share == req.share and brief.never == req.never
            assert not provider.requests
            with pytest.raises(CallRejected, match='fresh'):
                await engine.start(replace(brief, summary_en='changed'), req, None, None)
            assert not provider.requests
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_permission_fences_callee_publication_and_requester_answers(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            routes = call._routes
            assert not call.publication_allowed
            assert not engine._server._status[routes.status_token].relay_allowed()
            await call.emit(Utterance(call.request.request_id, 'before', 'callee', 'private', 0))
            assert not call._transcript
            await call._handle_tool('permission', {'decision': 'granted'}, 'consent1')
            assert call.publication_allowed
            generation = call.consent_generation
            await call.emit(Utterance(call.request.request_id, 'uncertain', 'callee',
                                      'uncertain echo', 1, delivery='unknown',
                                      consent_generation=generation))
            assert not any(row['item_id'] == 'uncertain' for row in call._transcript)
            call._hold.answer = AsyncMock(return_value=True)
            assert not await call.answer('q', 'yes', 99)
            call._hold.answer.assert_not_awaited()
            assert await call.answer('q', 'yes', 12)
            call._permission(False)
            assert call.consent_generation > generation and not call.publication_allowed
            await call.emit(Utterance(call.request.request_id, 'after', 'callee', 'private', 2))
            assert not any(row['item_id'] in ('before', 'after') for row in call._transcript)
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_capture_and_engine_queue_never_acquire_new_consent(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        gate = asyncio.Event()
        entered = asyncio.Event()
        try:
            call, brief, audio, events = await dial(engine, provider)
            await call._rt.handle_event({'type': EVENTS['speech_started'],
                                        'item_id': 'pregrant', 'audio_start_ms': 0})
            await call._handle_tool('permission', {'decision': 'granted'}, 'consent')
            await settled(lambda: call._rt._idle.is_set())
            before = len(peer.responses)
            await call._rt.handle_event({'type': EVENTS['input_transcript'],
                                        'item_id': 'pregrant', 'transcript': 'private before permission'})
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(peer.responses) == before
            assert not any(row['item_id'] == 'pregrant' for row in call._transcript)
            assert not any(isinstance(e, Utterance) and e.item_id == 'pregrant' for e in events.items)
            original_emit = events.emit

            async def block_status(event):
                if isinstance(event, StatusChanged) and event.detail == 'block':
                    entered.set()
                    await gate.wait()
                await original_emit(event)
            events.emit = block_status
            await call._emit(StatusChanged(call._request_id, CallState.IN_PROGRESS, 'block'))
            await entered.wait()
            source_generation = call.consent_generation
            await call.emit(Utterance(call._request_id, 'queued-old', 'callee', 'old source', 1,
                                      consent_generation=source_generation))
            call._permission(False)
            call._permission(True)
            gate.set()
            await call._event_queue.join()
            assert not any(isinstance(e, Utterance) and e.item_id == 'queued-old' for e in events.items)
            assert not any(row['item_id'] == 'queued-old' for row in call._transcript)
            await call.hang_up()
        finally:
            gate.set()
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_forced_session_cleanup_survives_stalled_stop(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            retained_rt = call._rt
            receive, watch = retained_rt._task, call._watch_task

            async def stalled():
                await asyncio.Event().wait()
            retained_rt.stop = stalled
            await asyncio.wait_for(call.hang_up(), 10)
            assert call._finished.is_set() and not engine._sessions
            assert not engine._server._status and not engine._server._media
            assert receive.done() and watch.done()
            assert retained_rt.brief is None and retained_rt._goal == ''
            assert retained_rt.events is None and retained_rt._on_tool_call is None
            assert not call.request.goal and not call._number
            assert not engine._cleanup_failed
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_transfer_renews_notice_and_closes_the_old_group_question(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            old_hold = call._hold
            assert await old_hold.ask('old-question', 'Which time?', (),
                                      consent_generation=call.consent_generation)
            question_id = old_hold.question.question_id
            await settled(lambda: call._rt._idle.is_set() and not old_hold._speech)
            generation = call.consent_generation
            before = len(peer.responses)
            peer.tool('permission', {'decision': 'transfer'}, 'transfer')
            await settled(lambda: call._hold is not old_hold and len(peer.responses) > before
                          and call._rt._conversation_open)
            assert not call.publication_allowed and call.consent_generation > generation
            assert old_hold._ended and old_hold.question is None
            assert call._hold is not old_hold and call._hold_started is None
            assert not await call.answer(question_id, 'noon', 12)
            requests = peer.responses[before:]
            assert len(requests) == 1
            assert brief.opener_ja in requests[0]['instructions']
            assert requests[0].get('output_modalities') != ['text']
            assert any(e.get('item', {}).get('call_id') == 'transfer'
                       for e in peer.sent if e['type'] == EVENTS['item_create'])
            await call._rt.handle_event({'type': EVENTS['speech_started'],
                                        'item_id': 'new-person', 'audio_start_ms': 1000})
            await call._rt.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'new-person'})
            await call._rt.handle_event({'type': EVENTS['input_transcript'],
                                        'item_id': 'new-person', 'transcript': 'はい、どうぞ。'})
            await settled(lambda: len(peer.responses) == before + 2)
            classifier = peer.responses[-1]
            assert classifier['output_modalities'] == ['text']
            assert classifier['tool_choice']['name'] == 'permission'
            assert not any(isinstance(e, Utterance) and e.item_id == 'new-person'
                           for e in events.items)
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_old_asr_cannot_grant_permission_for_a_new_recipient(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await answer_phone(call)
            await settled(lambda: call._rt._idle.is_set())
            await call._rt.handle_event({'type': EVENTS['speech_started'],
                                        'item_id': 'old', 'audio_start_ms': 100})
            await call._rt.handle_event({'type': EVENTS['speech_stopped'],
                                        'item_id': 'old', 'audio_end_ms': 200})
            before = len(peer.responses)
            peer.tool('permission', {'decision': 'transfer'}, 'transfer')
            await settled(lambda: len(peer.responses) > before and not call.publication_allowed
                          and call._rt._conversation_open and call._rt._idle.is_set())
            before = len(peer.responses)
            original = peer.send_json

            async def accept_current_reply(event):
                if (event['type'] == EVENTS['response_create']
                        and event['response'].get('output_modalities') == ['text']):
                    peer.sent.append(event)
                    peer.responses.append(event['response'])
                    resource = {'id': 'classifier', 'status': 'completed', 'usage': None,
                        'metadata': event['response']['metadata'], 'output': [{
                            'type': 'function_call', 'status': 'completed', 'name': 'permission',
                            'call_id': 'new-grant', 'arguments': json.dumps({'decision': 'granted'})}]}
                    peer.feed({'type': EVENTS['response_created'], 'response': resource})
                    peer.feed({'type': EVENTS['response_done'], 'response': resource})
                else:
                    await original(event)
            peer.send_json = accept_current_reply
            await call._rt.handle_event({'type': EVENTS['input_transcript'],
                                        'item_id': 'old', 'transcript': 'はい、どうぞ。'})
            for _ in range(100):
                await asyncio.sleep(0)
            assert len(peer.responses) == before and not call.publication_allowed
            relay_allowed = engine._server._status[call._routes.status_token].relay_allowed
            assert not relay_allowed()
            await call._rt.handle_event({'type': EVENTS['speech_started'],
                                        'item_id': 'new', 'audio_start_ms': 300})
            await call._rt.handle_event({'type': EVENTS['speech_stopped'],
                                        'item_id': 'new', 'audio_end_ms': 400})
            await call._rt.handle_event({'type': EVENTS['input_transcript'],
                                        'item_id': 'new', 'transcript': 'はい、どうぞ。'})
            await settled(lambda: call.publication_allowed)
            assert relay_allowed()
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_failed_hangup_keeps_recovery_and_restart_admission_blocked(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        call, brief, audio, events = await dial(engine, provider)
        token = call._routes.status_token
        engine._twilio.hang_up = AsyncMock(side_effect=TwilioError('synthetic failure'))
        await call.hang_up()
        final = next(e for e in events.items if isinstance(e, CallFinished))
        assert final.termination_confirmed is False
        assert engine.recovery_status()['admission'] == 'blocked'
        assert not engine._sessions
        assert not call.request.goal and not call._number
        await engine.close()
        await http.aclose()
        restarted, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            with pytest.raises(CallRejected, match='reconciliation'):
                await restarted.plan(request(request_id='next'))
            # Authenticated recovery callback routing does not need the old in-memory session.
            assert restarted._server.recovery_status(token, {
                'CallSid': SID, 'CallStatus': 'completed'})
            assert restarted.recovery_status()['admission'] == 'ready'
            assert not provider.requests
        finally:
            await restarted.close()
            await http.aclose()
    asyncio.run(run())


def test_billing_uses_no_bound_session_callback_and_releases_content(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        provider.prices = [None]
        try:
            call, brief, audio, events = await dial(engine, provider)
            reference = weakref.ref(call)
            await call.hang_up()
            assert engine._billing
            del call, brief, audio
            gc.collect()
            assert reference() is None
            assert events.items  # Collector stays live without retaining the session.
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())
