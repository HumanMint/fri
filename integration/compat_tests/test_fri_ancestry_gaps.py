"""Saved replies' ancestry, where this Fri can't follow it back (fri #387).

1. B quoted A, and A's deletion is queued, not yet taken by the controller. After a restart, or with B saved before this
   Fri started, what B drew on isn't known here, so nothing withheld B from the next request: A's words reached the
   model, and the delivery fence, which counts only what's taken back during a turn, let the reply go. A reply whose
   ancestry can't be told now stays out of a request while any withdrawal waits for the controller.
2. B drew on A, and C on B. B's record was pruned (capacity or age) while C's stayed: C named only B, so A's deletion
   during C's next turn went unseen. A reply's record now holds what its saved replies drew on, as far back as known.
3. A saved turn's `summaries` in a shape no controller writes, or with a generation missing or not a number, raised in
   the prospective summary check before anything withheld it. Such a turn leaves the request, before that check too.
"""
import asyncio
import time

import pytest

from compat_tests.test_fri_dependency_descendants import held_while_waiting
from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_endings import source
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from compat_tests.test_fri_panel_delivery import edits
from compat_tests.test_fri_panel_dependency_holds import MARKER, REPLY, quoting
from fri.fri_chat import ChatResult
from fri.fri_turn_withdrawal import SavedTurns


def asking(fri, tmp_path, reply=REPLY):
    """The model, replaced: what each request carried of the saved history, as text."""
    carried = []

    async def respond(text, context, ready):
        await ready()
        carried.append(str(context.get('turns')))
        return ChatResult(status='completed', reply=reply)
    model(fri, tmp_path, respond)
    return carried


# 1. Ancestry lost, a withdrawal still queued.

@pytest.mark.parametrize('queued', [True, False], ids=['its source queued', 'nothing'])
@pytest.mark.parametrize('how', ['saved before this Fri started', 'saved here, then a restart'])
def test_a_reply_whose_ancestry_is_lost_stays_out_while_a_withdrawal_is_queued(monkeypatch, tmp_path, how, queued):
    """The issue's repro: B, member 20's saved exchange, quoted A, and A's deletion is queued here, the controller not
    having taken it. What B drew on isn't known here: saved before this Fri started, or saved by it and lost with its
    memory in a restart (its withdrawal queue comes back from disk). Their next panel turn reads B: it stays out."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        fri.data = tmp_path
        quoted = source(age=1200, channel='1')
        said = quoting(store, fri, quoted['id'], tracked=how != 'saved before this Fri started')
        agent.saved_turns.kick = lambda: None  # Its withdrawal stays queued: the controller hasn't taken it.
        if queued:
            agent.saved_turns.withdraw([int(quoted['id'])], 123, deleted=True)
        if how == 'saved here, then a restart':
            agent.saved_turns = SavedTurns(agent)
            agent.saved_turns.kick = lambda: None
            assert (quoted['id'] in agent.saved_turns.pending) is queued and not agent.saved_turns.drawn
        carried = asking(fri, tmp_path)
        asked = panel(fri)
        try:
            await fri.chat_interaction(asked, 'say that again')
            assert carried and (MARKER in carried[-1]) is not queued
            assert (said['id'] in carried[-1]) is not queued
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_known_to_draw_on_nothing_queued_stays_in(monkeypatch, tmp_path):
    """What this Fri saved itself, its ancestry known, stays while an unrelated withdrawal waits: only what can't be
    told is held back for one."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        quoted = source(age=1200, channel='1')
        quoting(store, fri, quoted['id'], tracked=True)
        agent.saved_turns.kick = lambda: None
        elsewhere = int(source(age=900, channel='1')['id'])
        agent.saved_turns.withdraw([elsewhere], 123, deleted=True)
        carried = asking(fri, tmp_path)
        asked = panel(fri)
        try:
            await fri.chat_interaction(asked, 'say that again')
            assert MARKER in carried[-1]
        finally:
            await fri.close()
    asyncio.run(scenario())


# 2. A record pruned between two.

@pytest.mark.parametrize('gone', [True, False], ids=['the first quoted deleted', 'nothing'])
def test_a_reply_two_steps_from_a_deleted_message_is_held_after_the_middle_record_goes(monkeypatch, tmp_path, gone):
    """The issue's repro: B quoted A, and C, saved by this Fri, drew on B. B's record is pruned for capacity, and B
    leaves the six exchanges Gene keeps. A panel turn reads C, and A is deleted while its reply waits, its withdrawal
    timing out: held back, as when B's record was there."""
    async def scenario():
        monkeypatch.setattr('fri.fri_turn_withdrawal.MOST', 2)
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        quoted = source(age=1200, channel='1')
        middle = quoting(store, fri, quoted['id'], tracked=True)  # B, drawing on A.
        later = source(age=300, channel='456')
        store.save_chat('123', 'private:456', '20', 0, 'and again', 'still '+MARKER, 'model', 'high', source=later,
                        provenance=[], drew_on=[middle['id']])
        turns = agent.saved_turns
        turns.kept({'guild_id': '123', 'channel_id': '456', 'conversation_id': 'private:456', 'user_id': '20',
                    'revision': 0, 'source': later, 'drew_on': [middle['id']]}, {}, (time.time(), time.monotonic()))
        other = source(age=200, channel='456')  # A third record, so B's is pruned (MOST=2) and C's stays.
        turns.kept({'guild_id': '123', 'channel_id': '456', 'conversation_id': 'private:456', 'user_id': '20',
                    'revision': 0, 'source': other, 'drew_on': []}, {}, (time.time(), time.monotonic()))
        assert int(middle['id']) not in turns.drawn and int(later['id']) in turns.drawn
        with store.connection() as db:  # B leaves the six exchanges Gene keeps.
            db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (middle['id'],))
        carried = asking(fri, tmp_path)
        asked = panel(fri)
        try:
            saved = await held_while_waiting(fri, agent, asked, 'say that again', quoted if gone else None)
            assert MARKER in carried[-1] and middle['id'] not in carried[-1]  # It read C, not B.
            if gone:
                assert edits(asked, REPLY) == 0 and not saved and shown(asked) == skipped()
            else:
                assert REPLY in shown(asked) and saved
        finally:
            await fri.close()
    asyncio.run(scenario())


# 3. Summaries no controller writes, read before anything withholds them.

@pytest.mark.parametrize('summaries', [True, 3, [{'user_id': '20'}], [{'user_id': '20', 'generation': 'x'}],
                                       [{'user_id': '20', 'generation': -1}]],
                         ids=['true', 'a number', 'no generation', 'a generation not a number', 'a negative one'])
def test_an_ordinary_request_with_unreadable_summaries_leaves_that_turn_out(monkeypatch, tmp_path, summaries):
    """The issue's repro: a saved turn the controller answers with `summaries` as no controller writes them, then a
    normal panel question. The prospective summary check raised before anything withheld the turn: the question
    fizzled. It's answered now, without that turn."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        assert 'saved-dependencies' in fri.capabilities
        store.save_chat('123', 'private:456', '20', 0, 'what did ash say', 'ash said '+MARKER, 'model', 'high',
                        source=source(age=600, channel='456'), provenance=[])
        api = fri.api

        async def answering(method, path, payload=None):
            answer = await api(method, path, payload)
            if path == '/chat/context' and answer.get('turns'):
                answer['turns'][-1]['summaries'] = summaries
            return answer
        fri.api = answering
        carried = asking(fri, tmp_path, reply='sure')
        asked = panel(fri)
        try:
            await fri.chat_interaction(asked, 'what about tomorrow')
            assert carried and MARKER not in carried[-1] and 'sure' in shown(asked)
        finally:
            await fri.close()
    asyncio.run(scenario())
