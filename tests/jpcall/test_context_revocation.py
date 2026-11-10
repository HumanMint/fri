"""Revoked Realtime history is acknowledged deleted before the new notice/inference."""

import asyncio

from test_engine_flow import answer_phone, dial, make_engine, settled

from fri.jpcall.realtime import EVENTS


def test_transfer_deletes_remote_items_and_uses_an_empty_notice_input(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        try:
            call, *_ = await dial(engine, provider)
            await answer_phone(call)
            rt = call._rt
            remote = {}
            for item in ('callee-audio', 'callee-text', 'assistant-private'):
                remote[item] = {'id': item, 'text': 'revoked secret'}
                await rt.handle_event({'type': 'conversation.item.added', 'item': remote[item]})
            rt._recent_speech.append((0, 'assistant-private', 'revoked secret'))
            rt._partial_transcripts['assistant-private'] = 'revoked secret'
            blocked_delete = asyncio.Event()
            ack_delete = asyncio.Event()
            original = peer.send_json
            added_late = False

            async def send(event):
                nonlocal added_late
                if event['type'] == EVENTS['item_delete']:
                    blocked_delete.set()
                    await ack_delete.wait()
                    remote.pop(event['item_id'], None)
                    if not added_late:
                        added_late = True
                        remote['late-function'] = {'id': 'late-function', 'text': 'revoked secret'}
                        await rt.handle_event({'type': 'conversation.item.added', 'item': remote['late-function']})
                if event['type'] == EVENTS['response_create']:
                    assert not remote
                await original(event)
            peer.send_json = send
            start = len(peer.responses)
            peer.tool('permission', {'decision': 'transfer'}, 'transfer-context')
            await asyncio.wait_for(blocked_delete.wait(), 2)
            assert not call.publication_allowed and len(peer.responses) == start
            ack_delete.set()
            await settled(lambda: len(peer.responses) > start and rt._conversation_open)
            notice = peer.responses[start]
            assert notice['conversation'] == 'none' and notice['input'] == [] and notice['tools'] == []
            assert not remote and not rt._context_items and not rt._partial_transcripts
            assert 'revoked secret' not in repr(rt._recent_speech)
            assert set(remote).isdisjoint(rt._context_items)
            assert {'callee-audio', 'callee-text', 'assistant-private'} <= rt._deleted_items
            await rt.handle_event({'type': 'conversation.item.done', 'item': {'id': 'callee-audio'}})
            assert not rt._context_items  # A late finalization never resurrects deleted context.
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_missing_delete_ack_blocks_all_further_inference(tmp_path, monkeypatch):
    async def run():
        import pytest

        engine, provider, peer, http, *_ = await make_engine(tmp_path, monkeypatch)
        try:
            call, *_ = await dial(engine, provider)
            await answer_phone(call)
            rt = call._rt
            await rt.handle_event({'type': 'conversation.item.added', 'item': {'id': 'unconfirmed'}})
            original = peer.send_json

            async def send(event):
                if event['type'] != EVENTS['item_delete']:
                    await original(event)
            peer.send_json = send
            before = len(peer.responses)
            call._permission(False)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(rt.repeat_notice(), 6)
            assert len(peer.responses) == before
            assert 'unconfirmed' in rt._context_items
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())
