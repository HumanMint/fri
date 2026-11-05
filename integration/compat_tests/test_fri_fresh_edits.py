"""A fresh edit of an old message (fri #358 E1).

An edit is asked about by ID only while its message is young: a pin or an embed refresh repeats an earlier edit's time,
and asking about that would drop notes learned since. So a note saved today from a 100-day-old message stayed usable
after its author edited the message. An edit whose own time (Discord's) is fresh, within FRESH of Fri's clock or ahead
of it, is asked about by ID too, whatever its message's age, as a young message's edit is. One repeating an older time
is still left alone, and a deletion and a young message's edit are as before.
"""
import asyncio
import datetime as dt
import time
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_saved_withdrawal import SAVED, delete, edit
from compat_tests.test_fri_turn_withdrawal import world
from compat_tests.test_fri_withdrawal_old_sources import DAY, noted, old_message, only_queued
from compat_tests.test_fri_withdrawal_rollback import asked as requested
from compat_tests.test_fri_withdrawal_rollback import calls
from compat_tests.test_fri_withheld_history import LIKE, needs_sources

FRESH = 15*60  # As the queue's window (fri_turn_withdrawal.FRESH).


def edit_of(message, edited_at, user=20):
    """Discord's update of the member's message, edited at `edited_at`: a fresh edit, or a pin or embed refresh
    repeating an earlier edit's time, look the same."""
    return SimpleNamespace(id=message.id, author=SimpleNamespace(id=user, bot=False), content='never mind',
                           edited_at=dt.datetime.fromtimestamp(edited_at, dt.timezone.utc), attachments=[], embeds=[])


async def noted_from(monkeypatch, tmp_path, days):
    """A note learned now from member 20's message sent `days` ago, its exchange gone: only the note holds it."""
    w = await world(monkeypatch, tmp_path)
    try:
        needs_sources(w)
    except BaseException:
        await w.fri.close()
        raise
    message = old_message(days)
    noted(w, message)
    assert LIKE in await requested(w)
    only_queued(monkeypatch)
    return w, message


def kept(w):
    """Whether the controller still has the note."""
    return LIKE in str(w.store.chat_context('123', 'public:1', '20')['personal_memory']['notes'])


FRESH_EDITS = {'just now': 0, 'ten minutes ahead (Fri behind Discord)': 600, 'an hour ahead': 3600,
               'near the window\'s edge': -FRESH+60}


@pytest.mark.parametrize('when', list(FRESH_EDITS))
def test_the_repro_a_fresh_edit_of_an_old_message_takes_the_note_saved_from_it(monkeypatch, tmp_path, when):
    """The issue: a note learned today from a 100-day-old message, which its author then edits. Nothing was asked,
    and the next request had the note."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, 100)
        try:
            await edit(w, edit_of(message, time.time()+FRESH_EDITS[when]))
            assert [payload['message_ids'] for payload in calls(w, SAVED)] == [[str(message.id)]]
            assert LIKE not in await requested(w) and not kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['a pin repeating an edit five days old', 'just past the window'])
def test_an_update_repeating_an_older_edit_of_an_old_message_takes_nothing(monkeypatch, tmp_path, when):
    """A pin or an embed refresh repeats the time of an edit made long ago: nothing is asked, and the note learned
    since stays, as on main."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, 100)
        try:
            at = time.time()-(5*DAY if when.startswith('a pin') else FRESH+60)
            await edit(w, edit_of(message, at))
            assert not calls(w, SAVED) and str(message.id) not in w.agent.saved_turns.pending
            assert LIKE in await requested(w) and kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_young_messages_edit_is_asked_about_as_before(monkeypatch, tmp_path):
    """Whatever its edit's time, as main asks with none."""
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, 10)
        try:
            await edit(w, edit_of(message, time.time()-5*DAY))
            assert [payload['message_ids'] for payload in calls(w, SAVED)] == [[str(message.id)]]
            assert not kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_old_messages_deletion_is_asked_about_as_before(monkeypatch, tmp_path):
    async def scenario():
        w, message = await noted_from(monkeypatch, tmp_path, 100)
        try:
            await delete(w, message.id)
            assert [payload['message_ids'] for payload in calls(w, SAVED)] == [[str(message.id)]]
            assert LIKE not in await requested(w) and not kept(w)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
