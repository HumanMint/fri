"""Member summaries wait while Fri's withdrawal queue holds anything back (fri #268, #318): a claim names no message to
screen by, so nothing is claimed or sent until it holds nothing back, and the same work is built then."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_member_profiles import GAMING, GENERAL, run, world
from compat_tests.test_fri_member_summaries import settled, summarized, summarizer
from fri.fri_member_summary import build
from fri.fri_turn_withdrawal import SavedTurns

WORDS = ('fri raid tonight?', 'fri which build for it', 'fri the lavender bicycle plan stays between us')


async def exchanges(w):
    """Three readable exchanges of member 20's in #general, settled for a summary, and their messages."""
    events = []
    for text in WORDS:
        event = w.message(GENERAL, text)
        await w.say(GENERAL, text, event=event)
        events.append(event)
    settled(w)
    return events


def claimed(w):
    return [path for path, _ in w.calls if path == '/agent/member-summary/claim']


def test_a_summary_waits_while_the_withdrawal_record_cant_be_read(monkeypatch, tmp_path):
    """fri #318: the queue's record couldn't be read, so every saved context is held back (see fail_closed), yet the
    background builder sent the member's exchanges, a withdrawn one among them, to the summary model. It claims
    nothing while that lasts, and builds the same summary once it's over."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        from compat_tests.test_fri_withdrawal_rollback import module_clock
        w = world(monkeypatch, tmp_path)
        try:
            await exchanges(w)
            seen = summarizer(monkeypatch, w)
            agent = w.fri.agent
            (tmp_path/'withdrawals.json').write_text('{"pending": {')
            agent.saved_turns = SavedTurns(agent)
            assert agent.saved_turns.closed()
            assert await build(agent) is None and seen == [] and claimed(w) == []
            module_clock(monkeypatch, withdrawal.CLOSED_SECONDS)  # Held back no longer.
            agent.saved_turns = SavedTurns(agent)
            assert await build(agent) == {'kept': True} and len(seen) == 1
        finally:
            await w.fri.close()
    run(scenario)


def test_a_summary_waits_while_a_deleted_messages_withdrawal_is_still_queued(monkeypatch, tmp_path):
    """fri #268: a member deleted a message whose exchange Fri saved, and its withdrawal timed out, so it stayed queued
    while the controller still held the exchange. The builder sent its words to the summary model and kept the
    summary. It claims nothing while the withdrawal waits; once it lands, the summary is built without them."""
    async def scenario():
        import fri.fri_turn_withdrawal as withdrawal
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            seen = summarizer(monkeypatch, w)
            agent = w.fri.agent
            api = w.fri.api

            async def timing_out(method, path, payload=None):
                if path.startswith('/chat/turn/withdraw'):
                    raise TimeoutError()
                return await api(method, path, payload)

            w.fri.api = timing_out
            agent.saved_turns.withdraw([events[2].id], 123, deleted=True)  # Deleted; its withdrawal timed out.
            await asyncio.gather(*agent.saved_turns.tasks)
            assert str(events[2].id) in agent.saved_turns.pending
            assert await build(agent) is None and seen == [] and claimed(w) == []
            w.fri.api = api
            agent.saved_turns.retry()
            await asyncio.gather(*agent.saved_turns.tasks)
            assert not agent.saved_turns.pending
            monkeypatch.setattr(withdrawal, 'MARKED_SECONDS', 0)  # Past the hour a taken-back message stays held.
            assert await build(agent) == {'kept': True} and len(seen) == 1
            assert 'lavender' not in str(seen[0]['data']) and 'raid' in str(seen[0]['data'])
        finally:
            await w.fri.close()
    run(scenario)


def test_a_summary_waits_once_a_message_is_taken_back_while_it_was_claimed(monkeypatch, tmp_path):
    """The queue holds nothing back as the claim is made, and a message is taken back while the controller answers it.
    Checked again after that wait, nothing is sent, and the claim is let go as skipped, keeping nothing."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            seen = summarizer(monkeypatch, w)
            agent = w.fri.agent
            api, finished = w.fri.api, []

            async def taking_back(method, path, payload=None):
                if path.startswith('/chat/turn/withdraw'):
                    raise TimeoutError()
                answer = await api(method, path, payload)
                if path == '/agent/member-summary/claim':
                    agent.saved_turns.withdraw([events[2].id], 123, deleted=True)
                if path.endswith('/finish'):
                    finished.append(payload)
                return answer

            w.fri.api = taking_back
            assert await build(agent) == {'kept': False}
            assert seen == [] and [body['status'] for body in finished] == ['skipped']
        finally:
            await w.fri.close()
    run(scenario)


def timing_out(w):
    """The controller doesn't take any withdrawal: it still holds what was taken back."""
    api = w.fri.api

    async def calling(method, path, payload=None):
        if path.startswith('/chat/turn/withdraw'):
            raise TimeoutError()
        return await api(method, path, payload)
    w.fri.api = calling


def finished(w):
    return [payload for path, payload in w.calls if path.endswith('/finish') and path.startswith('/agent/member-summary')]


@pytest.mark.parametrize('when', ['while the model writes', 'while its cost is recorded'])
@pytest.mark.parametrize('how', ['a message of it deleted', 'every saved context held back'])
def test_what_a_summary_drew_on_taken_back_once_the_model_has_it_keeps_nothing(monkeypatch, tmp_path, when, how):
    """fri #333 item 1, the issue's repro: the build sent member 20's exchanges, the lavender one among them, and it
    was deleted while the model wrote (or while its usage receipt was recorded), its withdrawal timing out; or the
    withdrawal record turned unreadable then. The summary with it was kept, and a request in another channel got it
    while the withdrawal waited. Checked again after the model and after the receipt, nothing is kept, though what it
    cost is recorded."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            settled(w, 13*3600)
            agent = w.fri.agent
            timing_out(w)

            def take_back():
                if how == 'a message of it deleted':
                    agent.unsend(GENERAL, [events[2].id], 123)
                else:
                    (tmp_path/'withdrawals.json').write_text('{"pending": {')
                    agent.saved_turns = SavedTurns(agent)
                    assert agent.saved_turns.closed()

            seen = summarizer(monkeypatch, w, during=take_back if when == 'while the model writes' else None)
            if when == 'while its cost is recorded':
                agent.record.side_effect = lambda *args, **kwargs: take_back()
            assert await build(agent) == {'kept': False}
            assert len(seen) == 1 and agent.record.await_count == 1  # The model was asked, and what it cost counts,
            assert [body['status'] for body in finished(w)] == ['skipped']  # but nothing of it is kept,
            assert summarized(w, conversation=f'public:{GAMING}') is None  # nor reaches a request elsewhere.
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('change', ['deleted', 'edited'])
def test_a_message_changed_where_fri_never_heard_of_it_is_never_summarized(monkeypatch, tmp_path, change):
    """fri #333 item 2, the issue's repro: member 20's lavender message is deleted (or edited) in Discord, and Fri,
    restarted since, never hears of it, so nothing is queued. The builder sent it and kept the summary. The claim now
    names each exchange's messages, and each is read again from Discord first: that one is left out, and withdrawn,
    so the controller drops its exchange too; the others are summarized."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            agent = w.fri.agent
            if change == 'deleted':
                del w.posted[events[2].id]  # In Discord alone: no event reaches Fri.
            else:
                w.posted[events[2].id].content = 'never mind'
                w.posted[events[2].id].edited_at = dt.datetime.now(dt.timezone.utc)
            agent.saved_turns = SavedTurns(agent)  # Restarted, nothing queued.
            seen = summarizer(monkeypatch, w)
            assert await build(agent) == {'kept': True}
            assert len(seen) == 1 and 'lavender' not in str(seen[0]['data']) and 'raid' in str(seen[0]['data'])
            await asyncio.gather(*agent.saved_turns.tasks)
            assert 'lavender' not in str(w.store.chat_context('123', f'public:{GENERAL}', '20')['turns'])
        finally:
            await w.fri.close()
    run(scenario)


def test_an_earlier_summary_one_of_whose_messages_is_gone_isnt_carried_on(monkeypatch, tmp_path):
    """fri #333 item 2, its earlier summary: one built from the lavender message is carried on into the next, the
    message deleted in Discord since, unheard. The claim names what the earlier summary drew on too: it isn't sent, and
    the new summary replaces it."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            seen = summarizer(monkeypatch, w)
            agent = w.fri.agent
            assert await build(agent) == {'kept': True}
            for text in ('fri raid went fine', 'fri next one friday'):
                await w.say(GENERAL, text)
            settled(w)
            with w.store.connection() as db:
                db.execute('DELETE FROM fri_member_summary_calls')  # A day later.
            del w.posted[events[2].id]  # In Discord alone.
            assert await build(agent) == {'kept': True} and len(seen) == 2
            assert seen[1]['data']['previous_summary'] == ''
        finally:
            await w.fri.close()
    run(scenario)


@pytest.mark.parametrize('what', ['its exchanges', 'the earlier summary'])
def test_a_channel_no_longer_readable_while_its_messages_are_read_again_sends_nothing_of_it(monkeypatch, tmp_path, what):
    """Sol round 1 of #348, B2: #general was checked readable by everyone before its messages were read again from
    Discord, and restricted while that read was out; its exchanges (or the earlier summary from it) went to the model
    anyway. Readability is checked again once every read is back."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await exchanges(w)
            seen = summarizer(monkeypatch, w)
            agent = w.fri.agent
            if what == 'the earlier summary':
                assert await build(agent) == {'kept': True}
                for text in ('fri gaming tonight', 'fri which map'):
                    await w.say(GAMING, text)
                settled(w)
                with w.store.connection() as db:
                    db.execute('DELETE FROM fri_member_summary_calls')  # A day later.
            general = w.channels[GENERAL]
            fetch = general.fetch_message

            async def restricting(mid):
                w.readable[GENERAL] = False  # Restricted while the read is out.
                return await fetch(mid)

            general.fetch_message = restricting
            await build(agent)
            sent = seen[-1]['data'] if len(seen) > (what == 'the earlier summary') else {}
            assert 'raid' not in str(sent) and 'lavender' not in str(sent)
        finally:
            await w.fri.close()
    run(scenario)


def test_an_edit_before_the_save_never_withdraws_the_exchange(monkeypatch, tmp_path):
    """Sol round 1 of #348, B3: member 20's message was edited a second before its exchange was saved, and not since.
    The edit was read as one after the save (the save time less a margin), and its exchange and note withdrawn. One
    that can't be told apart may be left out of a build, but nothing of it is withdrawn."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            events = await exchanges(w)
            with w.store.connection() as db:
                saved_at = db.execute("SELECT at FROM chat_turns WHERE json_extract(source,'$.id')=?",
                                      (str(events[0].id),)).fetchone()[0]
            w.posted[events[0].id].edited_at = dt.datetime.fromtimestamp(saved_at-1, dt.timezone.utc)
            seen = summarizer(monkeypatch, w)
            agent = w.fri.agent
            await build(agent)
            await asyncio.gather(*agent.saved_turns.tasks)
            assert not agent.saved_turns.pending and str(events[0].id) not in agent.withdrawn
            assert 'raid tonight' in str(w.store.chat_context('123', f'public:{GENERAL}', '20')['turns'])
            assert len(seen) == 1 and 'which build' in str(seen[0]['data'])  # The others are summarized.
        finally:
            await w.fri.close()
    run(scenario)


def test_fri_health_says_summaries_wait_for_the_controller(monkeypatch, tmp_path):
    """Sol round 1 of #348, S1: against a controller that doesn't name a claim's messages, summaries paused without a
    word in fri health. It says so once a build was skipped for it."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            w.fri.chat = SimpleNamespace(headers=AsyncMock(return_value={'Authorization': 'Bearer test'}))
            w.fri.capabilities = w.fri.capabilities - {'summary-sources'}
            assert 'summary-sources' not in await w.fri.watcher.report()
            assert await build(w.fri.agent) is None
            assert any('summar' in line and 'summary-sources' in line
                       for line in (await w.fri.watcher.report()).splitlines())
        finally:
            await w.fri.close()
    run(scenario)


def test_a_controller_that_names_no_messages_gets_no_claim(monkeypatch, tmp_path):
    """Against a controller that doesn't name a claim's messages (before summary-sources), they can't be checked, so
    nothing is claimed or sent (fri #333)."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await exchanges(w)
            asked = []

            async def analyze(*args, **kwargs):
                asked.append(args)
                return {'summary': 'x'}, {}, 'completed'
            monkeypatch.setattr('fri.fri_member_summary.analyze', analyze)
            w.fri.chat = SimpleNamespace(headers=AsyncMock(return_value={'Authorization': 'Bearer test'}))
            w.fri.capabilities = w.fri.capabilities - {'summary-sources'}
            assert await build(w.fri.agent) is None and asked == [] and claimed(w) == []
        finally:
            await w.fri.close()
    run(scenario)
