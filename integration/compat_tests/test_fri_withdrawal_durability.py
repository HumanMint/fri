"""A withdrawal Gene hasn't taken is never dropped or released: not by a restart (fri #252), not by the queue's
capacity (fri #256). Against the real controller."""

import asyncio
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_saved_withdrawal import SAVED, asked, restarted
from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, settled, world
from compat_tests.test_fri_withheld_history import delete, earlier_exchange, failing, on_disk
from fri import fri_turn_withdrawal as withdrawal
from fri.fri_turn_withdrawal import SavedTurns


def capable(w):
    if 'saved-turn-withdrawal' not in w.fri.capabilities:
        pytest.skip('needs a controller offering saved-turn-withdrawal')


async def an_hour_later_asks(monkeypatch, w):
    """The local mark has run out, and the member asks something unrelated: what reaches the model."""
    monkeypatch.setattr(withdrawal, 'MARKED_SECONDS', 0)
    w.requests.clear()
    await w.agent.handle(w.said('fri hi'))
    assert w.requests
    return str(w.requests)


def test_a_failed_withdrawal_by_id_holds_its_message_back_across_a_restart(monkeypatch, tmp_path):
    """fri #252: Fri no longer knows where it saved the exchange (it saved it before a restart), so the deletion is
    asked about by ID, which times out; then Fri restarts before a retry succeeds. The withdrawal was in memory only,
    and the deleted words reached the next request. It's kept on disk as a hold: held back after the restart, never
    sent again from there, and naming no member."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        capable(w)
        try:
            earlier = await earlier_exchange(w)
            restarted(w)
            down = failing(w, SAVED)
            await delete(w, earlier, guild=123)
            assert str(earlier.id) in w.agent.saved_turns.pending
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again before a retry got through.
            down['down'] = False
            before = len(asked(w))
            w.agent.saved_turns.retry()
            await settled(w)
            assert str(earlier.id) in w.agent.saved_turns.pending  # Still held back.
            assert not any(str(earlier.id) in p['message_ids'] for p in asked(w)[before:])  # Never replayed.
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
            assert '"20"' not in on_disk(tmp_path)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_capacity_never_releases_a_withdrawal_gene_hasnt_taken(monkeypatch, tmp_path):
    """fri #256, with a cap of two: one failed withdrawal of an exchange Fri saved, then two unrelated deletions whose
    withdrawals fail too. The oldest was dropped for room, and once its hour-long mark ran out its words reached the
    next request. Capacity holds back more instead: every saved context, until what was dropped can't be kept any
    more, written down so a restart keeps it."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        capable(w)
        monkeypatch.setattr(withdrawal, 'MOST', 2)
        try:
            earlier = await earlier_exchange(w)
            failing(w, WITHDRAW)
            failing(w, SAVED)
            await delete(w, earlier, guild=123)
            for later in (5000, 6000):  # Newer messages, nearby in the channel: asked about by ID.
                await delete(w, SimpleNamespace(id=earlier.id+later), guild=123)
            await settled(w)
            assert len(w.agent.saved_turns.pending) <= 2
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again: still held back.
            assert w.agent.saved_turns.withheld([earlier.id]) == {str(earlier.id)}
            assert MARKER not in await an_hour_later_asks(monkeypatch, w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_the_queue_within_its_capacity_holds_nothing_more_back(monkeypatch, tmp_path):
    """Guard: under the cap, a withdrawal that fails holds back its own message only, as before."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        capable(w)
        try:
            earlier = await earlier_exchange(w)
            failing(w, WITHDRAW)
            await delete(w, earlier, guild=123)
            assert not w.agent.saved_turns.closed()
            assert w.agent.saved_turns.withheld([earlier.id, earlier.id+5000]) == {str(earlier.id)}
        finally:
            await w.fri.close()
    asyncio.run(scenario())
