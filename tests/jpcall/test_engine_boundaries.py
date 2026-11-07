import asyncio
import gc
import weakref
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_engine_flow import SID, dial, make_engine, request

from fri.jpcall.contract import CallFinished, CallRejected, Utterance
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
                                      'uncertain echo', 1, delivery='unknown'))
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
