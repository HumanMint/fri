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

