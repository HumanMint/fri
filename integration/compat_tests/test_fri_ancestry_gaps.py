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


# The edges, on SavedTurns alone.

def test_an_exchange_with_no_reply_stays_while_a_withdrawal_waits(monkeypatch, tmp_path):
    """Item 1 holds back only replies: a reaction's exchange, saved before this Fri started, repeats nothing but the
    member's own message, which is withheld itself if it's taken back."""
    fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
    fri.capabilities = frozenset(asyncio.run(fri.api('GET', '/fri/contract'))['capabilities'])
    turns = agent.saved_turns
    turns.kick = lambda: None
    turns.withdraw([int(source(age=900, channel='1')['id'])], 123, deleted=True)
    reacted = {'input': 'lol', 'reply': '', 'response_kind': 'react', 'burst': [],
               'source': {'kind': 'discord_message', 'id': source(age=300, channel='1')['id']}}
    said = {'input': 'and?', 'reply': 'and '+MARKER, 'burst': [],
            'source': {'kind': 'discord_message', 'id': source(age=200, channel='1')['id']}}
    context = {'turns': [reacted, said], 'pending': None, 'elsewhere': []}
    turns.withhold(context)
    assert context['turns'] == [reacted]
    asyncio.run(fri.close())


def test_a_reply_drawing_on_one_that_cant_be_told_still_cant_once_that_record_goes(monkeypatch, tmp_path):
    """Item 2 for what can't be told: R's record says it drew on what can't be told; C drew on R (R in the live view,
    say, so no saved exchange of the request said so). Once R's record is pruned, C's still says it."""
    monkeypatch.setattr('fri.fri_turn_withdrawal.MOST', 2)
    fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
    fri.capabilities = frozenset(asyncio.run(fri.api('GET', '/fri/contract'))['capabilities'])
    turns = agent.saved_turns

    def kept(age, drew, unknown=False):
        said = source(age=age, channel='1')
        turns.kept({'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20',
                    'revision': 0, 'source': said, 'drew_on': drew}, {}, (time.time(), time.monotonic()), None, unknown)
        return said['id']
    first = kept(900, [], unknown=True)
    later = kept(600, [first])
    kept(300, [])  # So the first record is pruned.
    assert int(first) not in turns.drawn
    assert turns.depends_on([later]) == ({first}, True)
    asyncio.run(fri.close())


# Sol round 1 of #399.

def chained(monkeypatch, tmp_path, most, length):
    """`length` replies saved here, each drawing on the one before, with MOST records kept. Returns the records and
    the newest reply's ID."""
    monkeypatch.setattr('fri.fri_turn_withdrawal.MOST', most)
    fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
    fri.capabilities = frozenset(asyncio.run(fri.api('GET', '/fri/contract'))['capabilities'])
    turns, previous = agent.saved_turns, None
    for n in range(length):
        said = source(age=length+10-n, channel='1')
        turns.kept({'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20',
                    'revision': 0, 'source': said, 'drew_on': [previous] if previous else []}, {},
                   (time.time(), time.monotonic()))
        previous = said['id']
    asyncio.run(fri.close())
    return turns.drawn, previous


@pytest.mark.parametrize('most', [2, 20000], ids=['two records kept', 'the default'])
def test_a_long_chain_keeps_its_ancestry_bounded(monkeypatch, tmp_path, most):
    """F2: 1,000 replies, each drawing on the one before. Pruning capped the records, but each kept all it inherited:
    the newest held 999 ancestors with two records kept, and the default kept half a million references. A record
    keeps at most a hundred dependency IDs; one that had to let some go says what it drew on can't all be told."""
    records, newest = chained(monkeypatch, tmp_path, most, 1000)
    assert len(records) == min(most, 1000)
    assert all(len(entry[2]) <= 100 for entry in records.values())
    assert sum(len(entry[2]) for entry in records.values()) <= len(records)*101
    assert records[int(newest)][3]  # Its ancestry was cut: what it drew on can't all be told.


def test_a_short_chain_keeps_all_of_its_ancestry(monkeypatch, tmp_path):
    """Under the cap, nothing is let go: the newest of 50 holds its 49 ancestors, and says it can tell."""
    records, newest = chained(monkeypatch, tmp_path, 20000, 50)
    assert len(records[int(newest)][2]) == 49 and not records[int(newest)][3]


def test_a_summary_isnt_cleared_for_a_reply_held_back_only_for_what_cant_be_told(monkeypatch, tmp_path):
    """F3: member 20's summary, and a saved reply of theirs this Fri can't trace, drawing on no summary. A withdrawal
    waits whose message member 30 wrote, so it can't be one of the summary's sources. The reply stays out while it
    waits (item 1), but the summary, which that withdrawal can't hold, stays as on main."""
    fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
    fri.capabilities = frozenset(asyncio.run(fri.api('GET', '/fri/contract'))['capabilities'])
    turns = agent.saved_turns
    turns.kick = lambda: None
    theirs = int(source(age=900, user='30', channel='1')['id'])
    turns.withdraw([theirs], 123, deleted=True, authors={theirs: 30})
    reply = {'input': 'and?', 'reply': 'and '+MARKER, 'burst': [],
             'source': {'kind': 'discord_message', 'id': source(age=200, channel='1')['id']}}
    summary = {'text': 'raids', 'refreshed': time.time()-3600}
    context = {'turns': [reply], 'pending': None, 'elsewhere': [], 'member_summary': summary}
    turns.withhold(context, member='20')
    assert context['turns'] == [] and context['member_summary'] == summary
    asyncio.run(fri.close())


def test_truncated_ancestry_is_held_while_a_withdrawal_waits(monkeypatch, tmp_path):
    records, newest = chained(monkeypatch, tmp_path, 20000, 150)
    other = tmp_path/'second'
    other.mkdir()
    fri, agent, *_ = environment(monkeypatch, other, channel_id=1)
    fri.capabilities = frozenset(asyncio.run(fri.api('GET', '/fri/contract'))['capabilities'])
    turns = agent.saved_turns
    turns.drawn = records
    turns.kick = lambda: None
    turns.withdraw([int(source(age=900, channel='1')['id'])], 123, deleted=True)
    context = {'turns': [{'reply': MARKER, 'source': {'id': newest}}]}
    turns.withhold(context)
    assert context['turns'] == []
    asyncio.run(fri.close())


def test_saving_a_long_chain_reads_only_each_direct_parents_cache(monkeypatch, tmp_path):
    class Counted(dict):
        reads = 0

        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)

        def get(self, key, default=None):
            self.reads += 1
            return super().get(key, default)

    fri, agent, *_ = environment(monkeypatch, tmp_path, channel_id=1)
    fri.capabilities = frozenset(asyncio.run(fri.api('GET', '/fri/contract'))['capabilities'])
    turns = agent.saved_turns
    turns.drawn = Counted()
    previous = None
    for n in range(1000):
        said = source(age=1100-n, channel='1')
        before = turns.drawn.reads
        turns.kept({'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '20',
                    'revision': 0, 'source': said, 'drew_on': [previous] if previous else []}, {},
                   (time.time(), time.monotonic()))
        assert turns.drawn.reads-before <= 1
        previous = said['id']
    assert turns.drawn.reads == 999
    assert len(turns.drawn) == 1000
    asyncio.run(fri.close())
