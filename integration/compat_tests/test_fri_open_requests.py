"""Fri's model context carries the speaker's own requests: titles in their DM, states only in public."""

import asyncio

import pytest

from compat_tests.test_fri_mentions import environment
from fri.fri_chat import ChatResult


@pytest.mark.parametrize(('channel', 'visibility', 'titled'), [(999, 'private', True), (1, 'public', False)])
def test_the_speakers_requests_reach_the_model(monkeypatch, tmp_path, channel, visibility, titled):
    async def scenario():
        fri, _, store, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=channel)
        own = store.request('123', '20', 'draft-1', '', 'Fix the modpack command\nwith details', [])
        store.request('123', '40', 'draft-2', '', 'Someone else entirely', [])
        event = message('fri is my modpack fix done yet')
        if visibility == 'private':
            event.guild = None
        fri.chat_once.return_value = ChatResult(status='completed', reply='still waiting on review')
        try:
            await fri.talk(123, channel, 20, event.content, visibility, event=event)
            requests = fri.chat_once.await_args.args[1]['requests']
            assert [row['id'] for row in requests] == [own['id']] and requests[0]['state'] == 'awaiting_owner'
            assert ('title' in requests[0]) is titled
        finally:
            await fri.close()
    asyncio.run(scenario())
