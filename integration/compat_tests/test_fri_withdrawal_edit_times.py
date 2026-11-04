"""An edit withdrawn by ID with the time Discord says it was made (fri #358 E1).

A deletion is asked about by ID whatever its message's age (#356), but an edit only while its message is young: the queue
had no edit time, and a pin or an embed refresh repeats an earlier edit's time, so asking about every edit would take
notes learned after that edit. A fresh edit of an old message left a note saved from it usable.

From a controller offering withdrawal-edit-times, the edit's time goes with it, whatever its message's age, and the
controller withdraws only what was certainly saved before it. What was saved after it stays: a pin repeating an old
edit's time, or an edit made before the save, takes nothing. What was saved too close to it to tell isn't withdrawn,
and Fri keeps the message out of use instead, held back as a hold. A controller without it gets main's request.
"""
import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_saved_withdrawal import SAVED, edit
from compat_tests.test_fri_turn_withdrawal import settled, world
from compat_tests.test_fri_withdrawal_old_sources import DAY, noted, old_message, only_queued
from compat_tests.test_fri_withdrawal_rollback import asked, calls
from compat_tests.test_fri_withheld_history import LIKE, failing, needs_sources, on_disk
from fri.fri_turn_withdrawal import SavedTurns

EDIT_TIMES = 'withdrawal-edit-times'
AGES = {'a 100-day-old message': 100, 'a 10-day-old message': 10}


def edit_of(message, edited_at):
    """Discord's update of member 20's message, edited at `edited_at`: a fresh edit, or a pin or embed refresh
    repeating an earlier edit's time, look the same."""
    return SimpleNamespace(id=message.id, author=SimpleNamespace(id=20, bot=False), content='never mind',
                           edited_at=dt.datetime.fromtimestamp(edited_at, dt.timezone.utc), attachments=[], embeds=[])


async def noted_from(monkeypatch, tmp_path, days, *, ago=0):
    """A world whose controller offers edit times, and a note learned `ago` seconds ago from a message `days` old."""
    w = await world(monkeypatch, tmp_path)
    needs_sources(w)
    if EDIT_TIMES not in w.fri.capabilities:
        await w.fri.close()
        pytest.skip('needs a controller offering '+EDIT_TIMES)
    message = old_message(days)
    noted(w, message)
    with w.store.connection() as db:
        db.execute('UPDATE personal_notes SET at=at-?,sources_at=sources_at-?', (ago, ago))
    assert LIKE in await asked(w)
    only_queued(monkeypatch)
    return w, message


def saved_at(w):
    with w.store.connection() as db:
        return db.execute('SELECT max(at) FROM personal_notes').fetchone()[0]


def kept(w):
    """Whether the controller still has the note, held back or not."""
    return LIKE in str(w.store.chat_context('123', 'public:1', '20')['personal_memory']['notes'])


@pytest.mark.parametrize('age', list(AGES))
def test_the_repro_a_fresh_edit_takes_the_note_saved_before_it(monkeypatch, tmp_path, age):
    """The issue: a note saved from a 100-day-old message, which its author then edits. No withdrawal was queued, and
    the next request had the note. The edit's time goes to the controller, which drops the note saved an hour before
    it. A young message's goes the same way, now with its time."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age], ago=3600)
        try:
            now = time.time()
            await edit(w, edit_of(message, now))
            assert [(payload['message_ids'], payload.get('edited_at')) for payload in calls(w, SAVED)] == [
                ([str(message.id)], pytest.approx(now, abs=1))]
            assert LIKE not in await asked(w) and not kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('age', list(AGES))
@pytest.mark.parametrize('when', ['a pin repeating an edit from days before the save',
                                  'an edit a minute before the save, its event late'])
def test_an_edit_before_the_save_takes_nothing(monkeypatch, tmp_path, age, when):
    """#356's reason to keep the age limit for edits: the note was learned from what the message says since the edit,
    and a pin or embed refresh repeats that edit's time. Nothing is taken, for a young message either, which main asked
    about with no time, so the controller dropped the note."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age])
        try:
            await edit(w, edit_of(message, saved_at(w)-(5*DAY if when.startswith('a pin') else 60)))
            assert LIKE in await asked(w) and kept(w)
            assert str(message.id) not in w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('age', list(AGES))
@pytest.mark.parametrize('seconds', [2, -2])
def test_an_edit_too_close_to_the_save_to_tell_keeps_the_note_out_of_use(monkeypatch, tmp_path, age, seconds):
    """Within seconds of the save, after it or before, the clocks can't tell which came first: it may be the very
    version saved. The controller withdraws nothing, and says so, and Fri holds the message back instead, after a
    restart too: the note stays with the controller, unused."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age])
        try:
            await edit(w, edit_of(message, saved_at(w)+seconds))
            assert kept(w) and LIKE not in await asked(w)
            assert w.agent.saved_turns.pending[str(message.id)].get('hold') is True
            w.agent.saved_turns = SavedTurns(w.agent)  # Restarted.
            assert LIKE not in await asked(w) and kept(w)
            assert json.loads(on_disk(tmp_path))['pending'][str(message.id)].get('hold') is True
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_later_edit_replaces_a_queued_one_and_an_earlier_time_never_narrows_it(monkeypatch, tmp_path):
    """Queued while the controller times out: the later of two edits is what's asked about, whatever order they came
    in, as what was saved between them held the first edit's words."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, 100, ago=3600)
        try:
            down, now = failing(w, SAVED), time.time()
            await edit(w, edit_of(message, now-600))
            await edit(w, edit_of(message, now))
            await edit(w, edit_of(message, now-1200))  # A pin repeating an earlier one.
            assert w.agent.saved_turns.pending[str(message.id)]['edited_at'] == pytest.approx(now, abs=1)
            down['down'] = False
            w.agent.saved_turns.retry()
            await settled(w)
            assert calls(w, SAVED)[-1]['edited_at'] == pytest.approx(now, abs=1)
            assert not kept(w) and not w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_deletion_after_an_edit_takes_everything(monkeypatch, tmp_path):
    async def scenario():
        from compat_tests.test_fri_withheld_history import delete
        w, message = await noted_from(monkeypatch, tmp_path, 100)
        try:
            down = failing(w, SAVED)
            await edit(w, edit_of(message, saved_at(w)-60))
            await delete(w, message, guild=123)
            entry = w.agent.saved_turns.pending[str(message.id)]
            assert entry['deleted'] is True and 'edited_at' not in entry
            down['down'] = False
            w.agent.saved_turns.retry()
            await settled(w)
            assert not kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_timed_edit_waits_for_a_controller_rolled_back_alone(monkeypatch, tmp_path):
    """Gene rolled back to one without edit times while the edit waits: it's never sent without its time, which would
    take what was saved after the edit too. It waits, held back, and goes once the controller offers them again."""
    async def scenario():
        from fri.fri_errors import ControllerRejected
        w, message = await noted_from(monkeypatch, tmp_path, 100, ago=3600)
        try:
            down = failing(w, SAVED)
            await edit(w, edit_of(message, time.time()))
            offered, api, rolled = w.fri.capabilities, w.fri.api, {'back': True}

            async def older(method, path, payload=None):  # As a controller without edit times answers.
                if rolled['back'] and path == '/fri/contract':
                    contract = await api(method, path, payload)
                    return {**contract, 'capabilities': [c for c in contract['capabilities'] if c != EDIT_TIMES]}
                if rolled['back'] and path == SAVED and 'edited_at' in (payload or {}):
                    raise ControllerRejected('Extra inputs are not permitted', 422)
                return await api(method, path, payload)

            w.fri.api, w.fri.capabilities = older, frozenset(offered)-{EDIT_TIMES}
            down['down'] = False
            sent = len(calls(w, SAVED))
            w.agent.saved_turns.retry()
            await settled(w)
            assert calls(w, SAVED)[sent:] == []  # Never sent without its time.
            assert str(message.id) in w.agent.saved_turns.pending and LIKE not in await asked(w) and kept(w)
            rolled['back'], w.fri.capabilities, w.agent.saved_turns.reread_at = False, offered, 0.0  # Forward again.
            w.agent.saved_turns.retry()
            await settled(w)
            assert not kept(w) and not w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('age', list(AGES))
def test_a_controller_without_edit_times_gets_mains_request(monkeypatch, tmp_path, age):
    """An old message's edit asks nothing, a young one's asks with no time, as on main."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age], ago=3600)
        try:
            w.fri.capabilities = frozenset(w.fri.capabilities)-{EDIT_TIMES}
            await edit(w, edit_of(message, time.time()))
            sent = calls(w, SAVED)
            if AGES[age] == 100:
                assert sent == [] and kept(w)
            else:
                assert [payload['message_ids'] for payload in sent] == [[str(message.id)]]
                assert 'edited_at' not in sent[0] and not kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
