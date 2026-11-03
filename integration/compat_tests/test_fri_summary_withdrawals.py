"""Member summaries wait while Fri's withdrawal queue holds anything back (fri #268, #318): a claim names no message to
screen by, so nothing is claimed or sent until it holds nothing back, and the same work is built then."""

import asyncio

from compat_tests.test_fri_member_profiles import GENERAL, run, world
from compat_tests.test_fri_member_summaries import settled, summarizer
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
