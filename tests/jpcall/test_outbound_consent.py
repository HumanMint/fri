"""Outbound model privacy and durable prepaid admission, using real engine seams."""

import asyncio
import json

import pytest
from test_engine_flow import answer_phone, dial, make_engine

from fri.jpcall.realtime import EVENTS


@pytest.mark.parametrize('decision', ['refused', 'transfer'])
def test_buffered_revocation_prevents_queued_translation_request(tmp_path, monkeypatch, decision):
    async def run():
        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        queued = []
        try:
            call, *_ = await dial(engine, provider)
            await answer_phone(call)
            spawn = call._spawn

            def enqueue(coro, *, translation=False):
                if translation:
                    queued.append(coro)
                else:
                    return spawn(coro)
            monkeypatch.setattr(call, '_spawn', enqueue)
            await call._rt.handle_event({'type': EVENTS['speech_started'], 'item_id': 'secret', 'audio_start_ms': 1000})
            await call._rt.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'secret'})
            await call._rt.handle_event({'type': EVENTS['input_transcript'], 'item_id': 'secret', 'transcript': '秘密の価格は1200円です'})
            assert queued
            resource = {'id': 'revocation', 'status': 'completed', 'output': [{
                'type': 'function_call', 'status': 'completed', 'name': 'permission', 'call_id': 'revoke',
                'arguments': json.dumps({'decision': decision})}]}
            await call._rt.handle_event({'type': EVENTS['response_created'], 'response': resource})
            await call._rt.handle_event({'type': EVENTS['response_done'], 'response': resource})
            assert not call.publication_allowed
            before = len([r for r in provider.requests if r.url.host == 'api.openai.com'])
            for coro in queued:
                await coro
            queued.clear()
            assert len([r for r in provider.requests if r.url.host == 'api.openai.com']) == before
        finally:
            for coro in queued:
                coro.close()
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_summary_rechecks_permission_after_settling(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        try:
            call, *_ = await dial(engine, provider)
            await answer_phone(call)
            call._transcript.append({'item_id': 'x', 'speaker': 'callee', 'text_ja': '秘密', 'started_at': 1})
            settled = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(call._summary(settled, asyncio.get_running_loop().time() + 5))
            await asyncio.sleep(0)
            call._permission(False)
            before = len(provider.requests)
            settled.set_result(None)
            assert await task is None and len(provider.requests) == before
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())



def test_revoked_completion_has_no_discord_memory_learning_or_timeline_trace(tmp_path, monkeypatch):
    async def run():
        import httpx
        from test_call_text_and_report import discord_call
        from test_engine_closing import callee_line
        from test_engine_flow import settled

        import fri.jpcall.engine as engine_module
        from fri.jpcall.contract import Translation
        from fri.jpcall.timeline import DebugTimeline

        marker = 'REVOKED_COMPLETION_SHOULD_NEVER_ESCAPE'
        path = tmp_path / 'private-debug.jsonl'
        timeline = DebugTimeline(path)
        monkeypatch.setattr(engine_module, 'DebugTimeline', lambda *args, **kwargs: timeline)
        engine, provider, peer, http, clock, setup, call = await discord_call(tmp_path, monkeypatch)
        session = call.session
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            await answer_phone(session)
            await settled(lambda: session._rt._idle.is_set() and not session._translations)
            original = provider.handle

            async def response(request):
                result = await original(request)
                if request.url.host == 'api.openai.com':
                    data = result.json()
                    data['output'][0]['content'][0]['text'] = marker
                    entered.set()
                    await release.wait()
                    return httpx.Response(200, json=data)
                return result
            http._transport.handler = response
            # Observe attempted engine publication as well as the real Discord adapter.
            delivered = []
            emit = session._emit

            async def observe(event):
                delivered.append(event)
                await emit(event)
            monkeypatch.setattr(session, '_emit', observe)
            await callee_line(session, 'private', '料金は千二百円です。')
            await asyncio.wait_for(entered.wait(), 2)
            generation = session.consent_generation
            session._permission(False)
            release.set()
            await settled(lambda: not session._translations)
            await session.hang_up()
            await setup.calls.flush_events()
            # No completion event means neither Discord nor downstream history/learning can ingest it.
            assert not any(isinstance(e, Translation) and marker in e.text_en for e in delivered)
            assert marker not in repr([(m.content, m.kwargs, m.edits) for m in setup.messages])
            assert not session._transcript and not session._outcome_hint
            assert all(r.url.host in ('api.openai.com', 'api.twilio.com') for r in provider.requests)
            for file in tmp_path.rglob('*'):
                if file.is_file():
                    assert marker.encode() not in file.read_bytes()  # Includes all local evidence/usage/debug files.
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            assert any(e['event'] == 'consent_revoked' and e['revoked_generation'] == generation for e in entries)
            assert any(e['event'] == 'transcript' and e['item_id'] == 'private'
                       and e['consent_generation'] == generation for e in entries)
        finally:
            release.set()
            await engine.close()
            await http.aclose()
    asyncio.run(run())
