"""Attention's Discord reads held up by Discord (a global rate-limit wait) never stall the heartbeat, and
are never cancelled."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from compat_tests.test_fri_fixes import GENE, message, world  # noqa: F401 (world is a fixture)
from fri import fri_fixes
from fri.fri_agent import Agent

QUESTION = 'anyone know why the build is broken?'


def row(mid):
    return {'id': str(mid), 'at': time.time(), 'gene_error': False, 'revision': 0, 'user_id': '11', 'text': QUESTION}


def test_a_held_ambient_pick_doesnt_stall_attention_and_still_goes_on(monkeypatch):
    monkeypatch.setattr('fri.fri_agent.ATTENTION_WAIT', 0.05)

    async def scenario():
        release, reads = asyncio.Event(), []

        def event(mid, channel):
            return SimpleNamespace(id=mid, guild=SimpleNamespace(id=123), channel=channel, content=QUESTION,
                                   author=SimpleNamespace(id=11, bot=False, display_name='friend'), mentions=[],
                                   reference=None, webhook_id=None)

        held, free = SimpleNamespace(id=456, parent_id=None), SimpleNamespace(id=457, parent_id=None)

        async def rate_limited(mid):  # discord.py's global rate-limit wait, which must never be cancelled.
            reads.append(mid)
            await release.wait()
            return event(mid, held)

        held.fetch_message = AsyncMock(side_effect=rate_limited)
        free.fetch_message = AsyncMock(side_effect=lambda mid: event(mid, free))
        channels = {456: held, 457: free}
        fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, get_channel=channels.get,
                              user=SimpleNamespace(id=99), busy=set())
        agent = Agent(fri)
        agent.state['config'].update(enabled=True, proactive=True)
        agent.state['channels'] = ['457']
        agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
        agent.respond = AsyncMock()
        agent.run = AsyncMock(side_effect=lambda *args, **kwargs: (
            {'action': 'reply', 'source_id': args[2]['fresh_ids'][-1], 'reason': ''}, {}, 'completed'))
        agent.recent[456].append(row(100))
        agent.recent[457].append(row(101))
        await asyncio.wait_for(agent.attend(), 1)
        assert reads == [100]
        agent.respond.assert_awaited_once()  # The other channel still got its turn.
        assert agent.respond.await_args.args[0].id == 101
        # The next pass leaves the channel whose reply is still waiting on Discord alone.
        agent.recent[456].append(row(102))
        agent.last_attention.clear()
        agent.run.reset_mock()
        await asyncio.wait_for(agent.attend(), 1)
        agent.run.assert_not_awaited()
        release.set()
        await asyncio.wait_for(asyncio.gather(*agent.attending), 1)
        assert reads == [100] and agent.respond.await_count == 2
        assert agent.respond.await_args.args[0].id == 100 and agent.respond.await_args.kwargs == {'ambient': True}
        assert not agent.ambient_replies
    asyncio.run(scenario())


def test_a_held_read_of_gene_error_files_it_for_owner_review_instead(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr('fri.fri_agent.ATTENTION_WAIT', 0.05)
    monkeypatch.setattr(fri_fixes, 'MARK', 0.01)

    async def scenario():
        agent = world.agent
        agent.state['auto_repairs_enabled'] = True
        error = message(GENE)
        world.found[error.id] = error
        release, reads = asyncio.Event(), []
        fetch = world.room.fetch_message.side_effect

        async def rate_limited(mid):
            reads.append(mid)
            await release.wait()
            return fetch(mid)

        world.room.fetch_message.side_effect = rate_limited
        agent.run = AsyncMock(return_value=({'summary': 'Evidence uncertain', 'proposed_request': 'Fix the stuck track'},
                                            {}, 'completed'))
        await asyncio.wait_for(agent.investigate(456, {'id': str(error.id), 'text': 'Track Stuck error'}), 1)
        paths = [call.args[1] for call in world.fri.api.await_args_list]
        assert '/requests' in paths and '/repairs' not in paths  # Not an automatic repair it couldn't check.
        [job_id] = json.loads((world.fri.data/'gene-fixes.json').read_text())
        assert world.store.get(job_id)['source_id'] == f'gene-error:{error.id}'
        release.set()
        await asyncio.wait_for(asyncio.gather(*agent.attending, *fri_fixes.MARKING), 1)
        assert reads.count(error.id) == 2  # Its own read and the 👀's, both carried through.
        assert '/repairs' not in [call.args[1] for call in world.fri.api.await_args_list]
        error.add_reaction.assert_awaited_once_with('👀')
    asyncio.run(scenario())
