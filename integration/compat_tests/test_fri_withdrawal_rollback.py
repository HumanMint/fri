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
    w.attempts = None
    if SOURCES not in w.fri.capabilities:
        return False
    api, w.attempts = w.fri.api, []
    async def older(method, path, payload=None):
        if method == 'POST':
            w.attempts.append((path, payload))
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


def attempts(w, path):
    """Every request Fri made to `path`, refused ones too (the emulation refuses before the harness records)."""
    made = w.attempts if getattr(w, 'attempts', None) is not None else w.calls
    return [payload for p, payload in made if p == path]


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


def test_a_withdrawal_left_on_disk_is_not_replayed_to_an_older_controller(monkeypatch, tmp_path):
    """B2 of the pre-release review: saved and timed out here, then Fri rolled back and the member erased ("forget me"),
    then this Fri started against a rolled-back Gene. Replaying it unguarded would write their IDs back. Sol round 1 of
    fri #250 (B1): it's neither sent nor dropped there, but kept, held back, until a Gene with the guard is known."""
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
            assert not attempts(w, WITHDRAW)
            assert str(earlier.id) in w.agent.saved_turns.pending  # Kept, and so held back.
            assert str(earlier.id) in w.agent.saved_turns.withheld([earlier.id])
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


# Sol round 1 of fri #250.

def older_from_the_start(w):
    """A Gene rolled back before Fri started: Fri reads its contract, so it knows no withdrawal-sources."""
    emulated = older_controller(w)
    w.fri.capabilities = w.fri.capabilities-NEWER
    return emulated


def test_a_restart_against_an_older_controller_keeps_a_valid_withdrawal_held_back(monkeypatch, tmp_path):
    """B1: saved against the older Gene, its withdrawal timed out, Fri restarted with the queue: it's neither sent
    unguarded nor dropped, so it stays held back past the hour. Once a Gene with the guard is known, it goes, with its
    revision. It's dropped only once it can no longer matter: older than an exchange lives."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            api = w.fri.api
            emulated = older_from_the_start(w)
            earlier = await earlier_exchange(w)
            state = failing(w, WITHDRAW)
            await delete(w, earlier)
            state['down'] = False
            w.agent.saved_turns = SavedTurns(w.agent)  # Started again, the queue intact.
            w.calls.clear()
            w.agent.saved_turns.retry()
            await settled(w)
            assert not attempts(w, WITHDRAW) and str(earlier.id) in w.agent.saved_turns.pending
            monkeypatch.setattr(withdrawal, 'MARKED_SECONDS', 0)
            w.requests.clear()
            await w.agent.handle(w.said('fri hi'))
            assert w.requests and MARKER not in str(w.requests)  # Still held back.
            if emulated:  # Gene rolled forward again: read on the retry schedule, then replayed with its revision.
                w.fri.api = api
                monkeypatch.setattr(withdrawal, 'REREAD_SECONDS', 0)
                w.agent.saved_turns.retry()
                await settled(w)
                assert [payload.get('revision') for payload in calls(w, WITHDRAW)] == [0]
                assert MARKER not in saved(w) and not w.agent.saved_turns.pending
            else:
                monkeypatch.setattr(withdrawal, 'KEPT_SECONDS', 0)  # Past the week an exchange lives: it can't matter.
                w.agent.saved_turns.retry()
                await settled(w)
                assert not w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def erased_ids(w, user='20'):
    with w.store.connection() as db:
        return db.execute('SELECT count(*) FROM chat_turn_withdrawals WHERE user_id=?', (user,)).fetchone()[0]


def test_a_replay_refused_by_a_rolled_back_gene_is_never_resent_unguarded(monkeypatch, tmp_path):
    """B2: on disk, its member erased by an older Fri, loaded while Fri knows the guard; Gene rolls back first, its 422
    makes Fri read the contract again, and the guard is gone: the entry waits, never resent without its revision."""
    async def scenario():
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        try:
            started = w.fri.capabilities | NEWER
            earlier = await earlier_exchange(w)
            state = failing(w, WITHDRAW)
            await delete(w, earlier)
            w.store.personal_control('20', 'disable')  # An older Fri's "forget me": the file stays.
            state['down'] = False
            older_controller(w)
            w.fri.capabilities = started  # Read at start, before Gene rolled back.
            w.calls.clear()
            w.agent.saved_turns = SavedTurns(w.agent)
            w.agent.saved_turns.retry()
            await settled(w)
            assert all('revision' in payload for payload in attempts(w, WITHDRAW))  # Never sent without it.
            assert erased_ids(w) == 0
            assert str(earlier.id) in w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_forget_me_while_fri_reads_the_contract_stops_the_resend(monkeypatch, tmp_path):
    """B3: this process's withdrawal is refused (Gene rolled back), and while Fri reads the contract again the member
    says "forget me": the resend is abandoned, so nothing of theirs is written after their erasure."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            started = w.fri.capabilities | NEWER
            earlier = await earlier_exchange(w)
            older_controller(w)
            w.fri.capabilities = started
            api = w.fri.api
            async def erasing(method, path, payload=None):
                if path == '/fri/contract':
                    w.store.personal_control('20', 'disable')
                    w.agent.forget(20, erased=True)
                return await api(method, path, payload)
            w.fri.api = erasing
            w.calls.clear()
            await delete(w, earlier)
            assert len(attempts(w, WITHDRAW)) == 1  # The refused one only.
            assert erased_ids(w) == 0 and not w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_erasure_during_the_contract_read_never_stops_the_others(monkeypatch, tmp_path):
    """B4: a by-ID withdrawal waiting (Gene has no such request since a rollback) is taken by its member's erasure
    while Fri reads the contract; the worker carries on, and another member's withdrawal still goes."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        w = await world(monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(withdrawal, 'REREAD_SECONDS', 0)
            started = w.fri.capabilities | NEWER
            theirs = w.said('fri explain '+MARKER, user=40)
            await w.agent.handle(theirs)
            assert MARKER in saved(w, '40')
            nearby = w.said('just chatting', user=30)
            w.agent.recent[1].append({'id': str(nearby.id), 'user_id': '30', 'name': 'member', 'text': nearby.content,
                                      'at': 0, 'revision': 0, 'gene_error': False})
            older_controller(w)
            w.fri.capabilities = started
            await delete(w, nearby, guild=123)  # By ID: its 404 makes Fri read the contract; it waits.
            assert str(nearby.id) in w.agent.saved_turns.pending
            state = failing(w, WITHDRAW)
            w.agent.looked.clear()
            w.agent.recent.clear()
            await delete(w, theirs)  # Member 40's: times out, so it waits too.
            state['down'] = False
            api, erased = w.fri.api, []
            async def erasing(method, path, payload=None):
                if path == '/fri/contract' and not erased:
                    erased.append(True)
                    w.store.personal_control('30', 'disable')
                    w.agent.forget(30, erased=True)
                return await api(method, path, payload)
            w.fri.api = erasing
            w.agent.saved_turns.retry()
            await settled(w)
            assert MARKER not in saved(w, '40')
            assert str(nearby.id) not in w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


# Sol round 2 of fri #250: an entry rebuilt from one an earlier process left keeps where it came from.

async def restored_edit(w, monkeypatch):
    """An edit's withdrawal timed out and was left on disk; the member erased through an older Fri (the file stays);
    Fri started again remembering the newer controller, which then rolled back."""
    from compat_tests.test_fri_saved_withdrawal import edit, edited
    from fri.fri_turn_withdrawal import SavedTurns
    started = w.fri.capabilities | NEWER
    earlier = await earlier_exchange(w)
    state = failing(w, WITHDRAW)
    await edit(w, edited(earlier))
    assert str(earlier.id) in on_disk(tmp_path_of(w))
    w.store.personal_control('20', 'disable')
    state['down'] = False
    w.agent.saved_turns = SavedTurns(w.agent)
    w.fri.capabilities = started
    older_controller(w)
    w.fri.capabilities = started  # Remembered from its start.
    w.calls.clear()
    if w.attempts is not None:
        w.attempts.clear()
    return earlier


def tmp_path_of(w):
    return w.fri.data


def test_a_deletion_after_a_restored_edit_is_never_sent_unguarded(monkeypatch, tmp_path):
    """The repro: the message's raw delete rebuilds its entry as a deletion. Gene refuses the newer fields (422), Fri
    reads the contract again, and the rebuilt entry is still the earlier process's: it waits, never resent without its
    revision, so nothing of the erased member is written."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            earlier = await restored_edit(w, monkeypatch)
            await delete(w, earlier, guild=123)
            assert erased_ids(w) == 0  # The live release writes the erased member's row for an unguarded one.
            assert all('revision' in payload for payload in attempts(w, WITHDRAW))
            assert str(earlier.id) in w.agent.saved_turns.pending
            assert str(earlier.id) in w.agent.saved_turns.withheld([earlier.id])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_save_here_naming_a_restored_entrys_message_keeps_it_an_earlier_ones(monkeypatch, tmp_path):
    """Another path that rebuilds it: this process saves an exchange with the same message (an edit answered again,
    say), then the message is deleted. The entry built from that save still came first from the earlier process."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            import time
            earlier = await restored_edit(w, monkeypatch)
            scope = {'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20'}
            w.agent.saved_turns.kept({**scope, 'revision': 1, 'source': {'kind': 'discord_message', 'id': str(earlier.id)}},
                                     {'ok': True}, (time.time(), time.monotonic()))
            await delete(w, earlier, guild=123)
            assert erased_ids(w) == 0  # The live release writes the erased member's row for an unguarded one.
            assert all('revision' in payload for payload in attempts(w, WITHDRAW))
            assert str(earlier.id) in w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_restored_entry_rebuilt_goes_with_its_revision_where_the_guard_is(monkeypatch, tmp_path):
    """Guard: against a controller with the guard, the rebuilt deletion is sent, with the revision first saved."""
    async def scenario():
        from compat_tests.test_fri_saved_withdrawal import edit, edited
        from fri.fri_turn_withdrawal import SavedTurns
        w = await world(monkeypatch, tmp_path)
        if SOURCES not in w.fri.capabilities:
            pytest.skip('needs a controller offering '+SOURCES)
        try:
            earlier = await earlier_exchange(w)
            state = failing(w, WITHDRAW)
            await edit(w, edited(earlier))
            state['down'] = False
            w.agent.saved_turns = SavedTurns(w.agent)
            w.calls.clear()
            await delete(w, earlier, guild=123)
            assert [(payload.get('deleted'), payload.get('revision')) for payload in calls(w, WITHDRAW)] == [(True, 0)]
            assert MARKER not in saved(w) and not w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())
