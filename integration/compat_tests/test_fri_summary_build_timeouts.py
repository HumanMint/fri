"""A member summary build whose Discord read times out or loses its connection lets its claim go (fri #369).

A build reads every message it would draw on again from Discord first (fri #333). A read Discord refused was caught,
so the build sent nothing and finished its claim as skipped. A read that timed out, or whose connection failed (a
reset, a disconnect), raised past that, out of the build, before the finish: the controller's one build claim stayed
pending, and no member's summary could be built until it expired ten minutes later. Those are caught beside Discord's
own errors now, as the recheck's are (fri #357): the build sends nothing, withdraws nothing, and lets the claim go.
"""
import asyncio
import json

import aiohttp
import discord
import pytest

from compat_tests.test_fri_member_profiles import GENERAL, run, world
from compat_tests.test_fri_member_summaries import settled, summarizer
from compat_tests.test_fri_summary_unknown_channel import taken
from compat_tests.test_fri_summary_withdrawals import exchanges
from fri.fri_member_summary import build
from fri.fri_turn_withdrawal import SavedTurns

FAILURES = {
    'a timeout': lambda: asyncio.TimeoutError(),
    'a lost connection': lambda: aiohttp.ClientConnectionError('connection lost'),
    'a server disconnect': lambda: aiohttp.ServerDisconnectedError(),
    'a reset': lambda: ConnectionResetError(54, 'Connection reset by peer'),
    "Discord's refusal": lambda: discord.ClientException('refused'),  # Caught before this, as the others are now.
}


def failing(channel, ids, failure):
    """The channel's reads of those messages by ID fail with `failure`; put back by the return."""
    real = channel.fetch_message

    async def reading(mid):
        if mid in ids:
            raise FAILURES[failure]()
        return await real(mid)

    channel.fetch_message = reading
    return lambda: setattr(channel, 'fetch_message', real)


def pending(w):
    """The controller's summary build claim still in force, or None."""
    with w.store.connection() as db:
        row = db.execute("SELECT value FROM agent_state WHERE key='member_summary_claim'").fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize('failure', list(FAILURES))
def test_a_build_whose_read_fails_lets_its_claim_go(monkeypatch, tmp_path, failure):
    """fri #369: member 20's lavender message can't be read again, the read failing with `failure`. Nothing goes to
    the model and nothing is withdrawn; the claim is finished as skipped, so the controller holds no claim, and once
    Discord answers again a later build summarizes all three exchanges."""
    async def scenario():
        found = taken(monkeypatch)
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            agent = w.fri.agent
            agent.saved_turns = SavedTurns(agent)  # Restarted, nothing queued.
            put_back = failing(w.fri.get_channel(GENERAL), {events[2].id}, failure)
            seen = summarizer(monkeypatch, w)
            assert await build(agent) == {'kept': False}
            settled(w)
            assert seen == [] and found == []
            assert pending(w) is None
            assert 'lavender' in str(w.store.chat_context('123', f'public:{GENERAL}', '20')['turns'])
            put_back()
            with w.store.connection() as db:
                db.execute('DELETE FROM fri_member_summary_calls')  # A day later.
            assert await build(agent) == {'kept': True}
            assert len(seen) == 1 and 'lavender' in str(seen[0]['data']) and 'raid' in str(seen[0]['data'])
        finally:
            await w.fri.close()
    run(scenario)
