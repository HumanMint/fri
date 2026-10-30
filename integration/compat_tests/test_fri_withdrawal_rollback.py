"""Withdrawals across a rollback of Gene or Fri (fri #224's pre-release review).

Against the real controller. With the paired one, a Gene rolled back to the live release is answered as that release
answers: no withdrawal-sources or saved-turn-withdrawal in its contract, a 422 for fields it doesn't know, no by-ID
endpoint. With a genuinely older controller (offline-checks with GENE_SHA set to it), its own answers are used.

B1. Gene rolls back while Fri runs: a withdrawal it refuses for its shape is sent again as it takes them, after Fri
    reads its contract again, and none is dropped for a refusal: it stays held back until the controller takes it.
B2. A withdrawal left on disk by an earlier process is replayed only to a controller offering the revision guard
    (withdrawal-sources). Any other one may have outlived the member's erasure by a rolled-back Fri, so it's dropped.
"""
import asyncio
import logging

import pytest

from compat_tests.test_fri_saved_withdrawal import SAVED
from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, saved, settled, world
from compat_tests.test_fri_withheld_history import delete, earlier_exchange, failing, on_disk
from fri.fri_errors import ControllerRejected

SOURCES = 'withdrawal-sources'
NEWER = {SOURCES, 'saved-turn-withdrawal'}  # What the live release doesn't offer, of what these use.


@pytest.fixture(autouse=True)
def statuses(monkeypatch):
    """A refusal carries its HTTP status, as Fri.api's does (the shared harness leaves it out)."""
    from starlette.testclient import TestClient

    import compat_tests.test_fri_mentions as harness
    last, request = {}, TestClient.request
    def answered(self, *args, **kwargs):
        response = request(self, *args, **kwargs)
        last['status'] = response.status_code
        return response
    monkeypatch.setattr(TestClient, 'request', answered)
    monkeypatch.setattr(harness, 'ControllerRejected', lambda detail: ControllerRejected(detail, last.get('status')))


def older_controller(w):
    """Answer as the live release does, unless the controller is one already. True if it's emulated here."""
    if SOURCES not in w.fri.capabilities:
        return False
    api = w.fri.api
    async def older(method, path, payload=None):
        if path == '/fri/contract':
            contract = await api(method, path, payload)
            return {**contract, 'capabilities': [c for c in contract['capabilities'] if c not in NEWER]}
        if path == WITHDRAW and {'deleted', 'revision'} & set(payload or {}):
            raise ControllerRejected('[{"type": "extra_forbidden"}]', 422)  # As its extra='forbid' model answers.
        if path == SAVED:
            raise ControllerRejected('Not Found', 404)
        return await api(method, path, payload)
    w.fri.api = older
    return True


def calls(w, path):
    return [payload for p, payload in w.calls if p == path]


def test_gene_rolled_back_while_fri_runs_still_takes_the_withdrawal(monkeypatch, tmp_path, caplog):
    """B1's repro: an exchange kept, Gene alone rolled back, its message deleted, the hour Fri holds it back over."""
    caplog.set_level(logging.DEBUG)
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            started = w.fri.capabilities | NEWER  # What Fri read as it started, against the newer controller.
            older_controller(w)
            w.fri.capabilities = started
            await delete(w, earlier)
            assert MARKER not in saved(w)  # Sent again as it takes them.
            assert not NEWER & w.fri.capabilities  # It read the contract again.
            monkeypatch.setattr('fri.fri_turn_withdrawal.MARKED_SECONDS', 0)  # The hour's hold over.
            w.requests.clear()
            await w.agent.handle(w.said('fri hi'))
            assert w.requests and MARKER not in str(w.requests)
            assert MARKER not in caplog.text
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_refused_withdrawal_is_never_dropped(monkeypatch, tmp_path):
    """Refused however it's sent: it stays queued, and held back, past the hour."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            api = w.fri.api
            async def refusing(method, path, payload=None):
                if path == WITHDRAW:
                    raise ControllerRejected('Use Fri in a configured server', 403)
                return await api(method, path, payload)
            w.fri.api = refusing
            await delete(w, earlier)
            assert str(earlier.id) in w.agent.saved_turns.pending
            monkeypatch.setattr('fri.fri_turn_withdrawal.MARKED_SECONDS', 0)
            w.requests.clear()
            await w.agent.handle(w.said('fri hi'))
            assert w.requests and MARKER not in str(w.requests)  # Still held back.
            w.fri.api = api
            w.agent.saved_turns.retry()
            await settled(w)
            assert MARKER not in saved(w) and not w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_by_id_ones_a_rolled_back_controller_cant_take_wait_held_back(monkeypatch, tmp_path):
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            started = w.fri.capabilities | NEWER
            older_controller(w)
            w.fri.capabilities = started
            nearby = w.said('just chatting')
            await delete(w, nearby, guild=123)  # Asked by ID; it has no such request.
            assert str(nearby.id) in w.agent.saved_turns.pending
            assert str(nearby.id) in w.agent.saved_turns.withheld([nearby.id])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_withdrawal_left_on_disk_is_not_replayed_to_an_older_controller(monkeypatch, tmp_path, caplog):
    """B2's repro: saved and timed out here, then Fri rolled back and the member erased ("forget me"), then this Fri
    started against a rolled-back Gene. Replaying it would write their IDs back; it's dropped instead."""
    caplog.set_level(logging.DEBUG)
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            state = failing(w, WITHDRAW)
            await delete(w, earlier)
            assert str(earlier.id) in on_disk(tmp_path)
            w.store.personal_control('20', 'disable')  # A rolled-back Fri's "forget me": the file stays.
            state['down'] = False
            older_controller(w)
            w.fri.capabilities = frozenset((await w.fri.api('GET', '/fri/contract'))['capabilities'])  # Its start.
            w.calls.clear()
            w.agent.saved_turns = SavedTurns(w.agent)
            w.agent.saved_turns.retry()
            await settled(w)
            with w.store.connection() as db:  # A controller without the revision guard writes the erased IDs back.
                assert db.execute("SELECT count(*) FROM chat_turn_withdrawals WHERE user_id='20'").fetchone()[0] == 0
            assert not calls(w, WITHDRAW)
            assert str(earlier.id) not in on_disk(tmp_path) and not w.agent.saved_turns.pending
            assert any('earlier process' in line and str(earlier.id) in line for line in caplog.messages)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_withdrawal_left_on_disk_is_replayed_with_its_revision_where_the_guard_is(monkeypatch, tmp_path):
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        if SOURCES not in w.fri.capabilities:
            pytest.skip('needs a controller offering '+SOURCES)
        try:
            earlier = await earlier_exchange(w)
            state = failing(w, WITHDRAW)
            await delete(w, earlier)
            state['down'] = False
            w.calls.clear()
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again against the same controller.
            w.agent.saved_turns.retry()
            await settled(w)
            assert [payload.get('revision') for payload in calls(w, WITHDRAW)] == [0]
            assert MARKER not in saved(w) and str(earlier.id) not in on_disk(tmp_path)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_ordinary_withdrawal_sends_as_before(monkeypatch, tmp_path):
    """Guard: with the controller Fri started against, one call, the shape it offers, no contract read again."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await earlier_exchange(w)
            w.calls.clear()
            await delete(w, earlier)
            assert len(calls(w, WITHDRAW)) == 1 and not calls(w, '/fri/contract')
            assert MARKER not in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())

