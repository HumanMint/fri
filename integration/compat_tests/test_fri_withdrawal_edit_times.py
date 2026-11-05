"""An edit withdrawn by ID with the time Discord says it was made (fri #358 E1).

A deletion is asked about by ID whatever its message's age (#356), but an edit only while its message is young: the queue
had no edit time, and a pin or an embed refresh repeats an earlier edit's time, so asking about every edit would take
notes learned after that edit. A fresh edit of an old message left a note saved from it usable.

From a controller offering withdrawal-edit-times, the edit's time goes with it, whatever its message's age. Each save
names which version of its messages it was made from, on Discord's clock (Sol round 1 of #364, B2), and the controller
places the edit against that: one saved from an earlier version goes, one from that version or a later one stays (a pin,
an embed refresh, a late event). One it can't place (an older Fri's save of an old message, or saves of both kinds) it
withdraws nothing of, and Fri keeps the message out of use instead, as a hold; its author's summary that drew on it is
held back too (B1). A deletion of a held message still goes (S1), and an exchange Fri saved itself is placed against the
version it answered (P1). A controller without edit times gets main's request.
"""
import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_saved_withdrawal import SAVED, asked, edit
from compat_tests.test_fri_turn_withdrawal import MARKER, WITHDRAW, saved, settled, world
from compat_tests.test_fri_withdrawal_old_sources import DAY, old_message, only_queued
from compat_tests.test_fri_withdrawal_rollback import asked as requested
from compat_tests.test_fri_withdrawal_rollback import calls
from compat_tests.test_fri_withheld_history import LIKE, delete, failing, needs_sources, on_disk
from fri.fri_turn_withdrawal import SavedTurns, sent_at

EDIT_TIMES = 'withdrawal-edit-times'
AGES = {'a 100-day-old message': 100, 'a 10-day-old message': 10}


def edit_of(message, edited_at, user=20):
    """Discord's update of the member's message, edited at `edited_at`: a fresh edit, or a pin or embed refresh
    repeating an earlier edit's time, look the same."""
    return SimpleNamespace(id=message.id, author=SimpleNamespace(id=user, bot=False), content='never mind',
                           edited_at=dt.datetime.fromtimestamp(edited_at, dt.timezone.utc), attachments=[], embeds=[])


def edit_times(w):
    needs_sources(w)
    if EDIT_TIMES not in w.fri.capabilities:
        pytest.skip('needs a controller offering '+EDIT_TIMES)


def noted(w, message, version=None):
    """A preference learned now from member 20's message, by a Fri that names the version of it the note came from
    (`version`; when it was sent by default), or by an older one that names none (False). Only the note holds it."""
    source = {'kind': 'discord_message', 'id': str(message.id), 'user_id': '20', 'channel_id': '1', 'guild_id': '123',
              'display_name': 'member'}
    seen = sent_at(message.id) if version is None else version
    w.store.save_chat('123', 'public:1', '20', 0, LIKE, 'okay', 'model', 'high', source=source, provenance=[],
                      notes=[{'category': 'hobbies', 'evidence': LIKE}],
                      **({} if version is False else {'versions': {str(message.id): seen}}))
    with w.store.connection() as db:
        db.execute('DELETE FROM chat_turns')


async def noted_from(monkeypatch, tmp_path, days, version=None):
    """A world whose controller offers edit times, and a note learned now from a message `days` old."""
    w = await world(monkeypatch, tmp_path)
    try:
        edit_times(w)
    except BaseException:
        await w.fri.close()
        raise
    message = old_message(days)
    noted(w, message, version)
    assert LIKE in await requested(w)
    only_queued(monkeypatch)
    return w, message


def kept(w):
    """Whether the controller still has the note, held back or not."""
    return LIKE in str(w.store.chat_context('123', 'public:1', '20')['personal_memory']['notes'])


@pytest.mark.parametrize('age', list(AGES))
def test_the_repro_a_fresh_edit_takes_the_note_saved_from_before_it(monkeypatch, tmp_path, age):
    """The issue: a note learned today from a 100-day-old message, which its author then edits. No withdrawal was
    queued, and the next request had the note. The edit's time goes to the controller, which drops the note learned from
    the version before it. A young message's goes the same way, now with its time."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age])
        try:
            now = time.time()
            await edit(w, edit_of(message, now))
            assert [(payload['message_ids'], payload.get('edited_at')) for payload in calls(w, SAVED)] == [
                ([str(message.id)], pytest.approx(now, abs=1))]
            assert LIKE not in await requested(w) and not kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('age', list(AGES))
@pytest.mark.parametrize('when', ['a pin repeating the edit the note was learned from',
                                  'a late event of an edit before that one'])
def test_an_edit_no_later_than_the_version_saved_takes_nothing(monkeypatch, tmp_path, age, when):
    """#356's reason to keep the age limit for edits: the note was learned from what the message says since it was
    edited five days ago, and a pin or embed refresh repeats that edit's time. Nothing is taken, for a young message
    either, which main asked about with no time, so the controller dropped the note."""
    async def scenario():
        version = time.time()-5*DAY
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age], version)
        try:
            await edit(w, edit_of(message, version if when.startswith('a pin') else version-3600))
            assert LIKE in await requested(w) and kept(w)
            assert str(message.id) not in w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('age', list(AGES))
def test_an_older_fris_note_is_placed_as_before(monkeypatch, tmp_path, age):
    """Saved by a Fri that named no version: a young message's edit takes it, as an edit with no time took it before;
    an old one's can't be placed against it, so the controller withdraws nothing and Fri holds the message back instead,
    after a restart too, and the note stays with the controller, unused."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age], version=False)
        try:
            await edit(w, edit_of(message, time.time()))
            if AGES[age] == 10:
                assert not kept(w) and not w.agent.saved_turns.pending
                return
            assert kept(w) and LIKE not in await requested(w)
            assert w.agent.saved_turns.pending[str(message.id)].get('hold') is True
            w.agent.saved_turns = SavedTurns(w.agent)  # Restarted.
            assert LIKE not in await requested(w) and kept(w)
            assert json.loads(on_disk(tmp_path))['pending'][str(message.id)].get('hold') is True
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_s1_a_deletion_of_a_message_held_back_since_a_restart_takes_it(monkeypatch, tmp_path):
    """Sol round 1's S1: the hold an unsure edit left goes to disk without its server, so after a restart a deletion
    of its message withdrew nothing and the note stayed with the controller. The deletion names the server, and goes."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, 100, version=False)
        try:
            await edit(w, edit_of(message, time.time()))
            w.agent.saved_turns = SavedTurns(w.agent)  # Restarted: held from disk, with no server.
            assert w.agent.saved_turns.pending[str(message.id)].get('hold') is True and kept(w)
            await delete(w, message, guild=123)
            assert not kept(w) and not w.agent.saved_turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_later_edit_replaces_a_queued_one_and_an_earlier_time_never_narrows_it(monkeypatch, tmp_path):
    """Queued while the controller times out: the later of two edits is what's asked about, whatever order they came
    in, as what was saved between them held the first edit's words."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, 100)
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
        version = time.time()-5*DAY
        w, message = await noted_from(monkeypatch, tmp_path, 100, version)
        try:
            down = failing(w, SAVED)
            await edit(w, edit_of(message, version))
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
        w, message = await noted_from(monkeypatch, tmp_path, 100)
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
            assert str(message.id) in w.agent.saved_turns.pending and LIKE not in await requested(w) and kept(w)
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
        w, message = await noted_from(monkeypatch, tmp_path, AGES[age])
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


# The versions a save names, and Fri's own exchange (P1).

@pytest.mark.parametrize('offered', [True, False])
def test_a_turn_names_the_version_of_the_message_it_answered(monkeypatch, tmp_path, offered):
    """To a controller offering edit times; one without them gets the turn as before."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            edit_times(w)
            if not offered:
                w.fri.capabilities = frozenset(w.fri.capabilities)-{EDIT_TIMES}
            said = w.said('fri explain '+MARKER)
            await w.agent.handle(said)
            [turn] = calls(w, '/chat/turn')
            assert turn.get('versions') == ({str(said.id): sent_at(said.id)} if offered else None)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_p1_an_edit_no_later_than_the_version_fri_answered_leaves_its_exchange(monkeypatch, tmp_path):
    """Sol round 1's P1 (pre-existing): an exchange Fri saved itself was withdrawn by its scope on any edit event, so a
    pin repeating the time of the version it answered deleted a valid exchange. That one leaves it; a later edit takes
    it, as before. Only with a controller offering edit times, which places the rest by ID (see round 2's B1 below);
    one without them takes everything, as before."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            edit_times(w)
            said = w.said('fri explain '+MARKER)
            await w.agent.handle(said)
            assert MARKER in saved(w)
            await edit(w, edit_of(said, sent_at(said.id)))
            assert not asked(w, WITHDRAW) and MARKER in saved(w)
            await edit(w, edit_of(said, sent_at(said.id)+60))
            assert asked(w, WITHDRAW) and MARKER not in saved(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def exchange_from(w, message, version):
    """Member 20's exchange answering the message as it read at `version`, saved by the controller and tracked by Fri
    as its own (see SavedTurns.kept)."""
    source = {'kind': 'discord_message', 'id': str(message.id), 'user_id': '20', 'channel_id': '1', 'guild_id': '123',
              'display_name': 'member'}
    began = time.time(), time.monotonic()
    w.store.save_chat('123', 'public:1', '20', 0, 'what about dinner', 'okay', 'model', 'high', source=source,
                      provenance=[], versions={str(message.id): version})
    payload = {'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20', 'revision': 0,
               'source': source}
    w.agent.saved_turns.kept(payload, {}, began, None, {str(message.id): version})


@pytest.mark.parametrize('age', ['a 100-day-old message', 'a 60-second-old message'])
def test_r2_b1_fris_own_exchange_vouches_only_for_itself(monkeypatch, tmp_path, age):
    """Sol round 2's B1: a note learned from the message as it first read, its exchange trimmed since, and an exchange
    Fri saved itself, and tracks, from a later edit. A pin repeating that edit's time matched Fri's exchange, so nothing
    was asked, and the note from before the edit reached the next request. Fri's exchange stays, and the rest is asked
    about by ID with the edit's time: the controller can't keep the note, so the message is held back."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        try:
            edit_times(w)
            message = old_message(100 if age.startswith('a 100') else 60/DAY)
            first = sent_at(message.id)
            later = first+(DAY if age.startswith('a 100') else 30)
            noted(w, message, first)
            exchange_from(w, message, later)
            only_queued(monkeypatch)
            assert LIKE in await requested(w)
            await edit(w, edit_of(message, later))
            assert [(payload['message_ids'], payload.get('edited_at')) for payload in calls(w, SAVED)] == [
                ([str(message.id)], pytest.approx(later, abs=1))]
            assert not asked(w, WITHDRAW)  # Fri's own exchange isn't taken by its scope.
            assert LIKE not in await requested(w)
            assert w.agent.saved_turns.pending[str(message.id)].get('hold') is True
        finally:
            await w.fri.close()
    asyncio.run(scenario())
