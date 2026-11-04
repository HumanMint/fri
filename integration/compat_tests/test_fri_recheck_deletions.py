"""A recheck that finds a message deleted takes back what was saved from it, and a hold past its time stops holding
back as soon as anything asks (fri #358, R1 and L1).

R1: a learning pass, an archive pass, a read (its window or the pins) and an archive search each read what they hold
again before it goes anywhere. One found deleted there, Discord's 404 for its ID, was only left out: no withdrawal was
queued, so a note saved from it earlier stayed usable, here and in Gene. It's now taken back as its delete event, which
never came, would have taken it. One only missing from a list (unpinned, out of the window), or one Discord won't say
about (a 403, a failure), is left out as before, and nothing is withdrawn for it.

L1: a hold read from disk stayed pending past its deadline, and so held back what waits on the queue (member summaries,
memory views), until another withdrawal, a saved turn or a restart pruned the queue.
"""
import asyncio
from types import SimpleNamespace

import discord
import pytest

from compat_tests.test_fri_history_search import query, setup
from compat_tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from compat_tests.test_fri_looking import read_harness
from compat_tests.test_fri_panel_and_learning_fences import batch
from compat_tests.test_fri_pass_withdrawals import explorer
from compat_tests.test_fri_pins import pinned
from compat_tests.test_fri_saved_withdrawal import SAVED, SAVED_CAPABILITY
from compat_tests.test_fri_turn_withdrawal import world
from compat_tests.test_fri_withdrawal_old_sources import DAY, old_message, only_queued
from compat_tests.test_fri_withdrawal_rollback import module_clock
from compat_tests.test_fri_withheld_history import delete, failing, needs_sources
from fri.fri_actions import Actions
from fri.fri_agent import Agent
from fri.fri_turn_withdrawal import KEPT_SECONDS, NOTED_SECONDS, SOURCES, SavedTurns

TRANCE = 'I love trance'  # Member 11's message in the learning batch, and what Fri learned from it earlier.
UNCONFIRMED = ['refused', 'failing', 'timing out']


def taken(monkeypatch):
    """What the agent takes back as deleted (Agent.unsend): (channel, message IDs, the server it's withdrawn under)."""
    found, original = [], Agent.unsend

    def unsend(self, channel, ids, saved_in=None):
        found.append((channel, sorted(int(i) for i in ids), saved_in))
        return original(self, channel, ids, saved_in)

    monkeypatch.setattr(Agent, 'unsend', unsend)
    return found


def answer(how):
    """A by-ID read Discord doesn't answer with a 404: it won't say, or can't."""
    if how == 'refused':
        raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing Access')
    if how == 'failing':
        raise discord.HTTPException(SimpleNamespace(status=503, reason='Service Unavailable'), 'try again')
    raise TimeoutError()


# R1 on a learning pass (Agent.learn).

async def every_capability(agent):
    """The controller's whole contract, so a withdrawal by ID goes to it as in production (batch offers only
    learning-withdrawals)."""
    capabilities = frozenset((await agent.fri.api('GET', '/fri/contract'))['capabilities'])
    if not {SAVED_CAPABILITY, SOURCES} <= capabilities:
        pytest.skip('needs a controller offering '+SAVED_CAPABILITY+' and '+SOURCES)
    agent.fri.capabilities = capabilities


def noted(store, message_id):
    """A preference member 11 taught Fri earlier from that message: only the note holds its words now."""
    source = {'kind': 'discord_message', 'id': message_id, 'user_id': '11', 'channel_id': '1', 'guild_id': '123',
              'display_name': 'member'}
    store.save_chat('123', 'public:1', '11', 0, TRANCE, 'okay', 'model', 'high', source=source, provenance=[],
                    notes=[{'category': 'music', 'evidence': TRANCE}])
    with store.connection() as db:
        db.execute('DELETE FROM chat_turns')


def notes(store):
    return [note['evidence'] for note in store.chat_context('123', 'public:1', '11')['personal_memory']['notes']]


def recorded(agent):
    """The controller requests from here on: a path's payloads."""
    api, calls = agent.fri.api, []

    async def calling(method, path, payload=None):
        calls.append((path, payload))
        return await api(method, path, payload)

    agent.fri.api = calling
    return lambda path: [payload for called, payload in calls if called == path]


async def drained(agent):
    while agent.saved_turns.tasks:
        await asyncio.gather(*list(agent.saved_turns.tasks))


def sent_ids(asked):
    return [[record['id'] for record in data['messages']] for data in asked]


def test_the_repro_a_learning_pass_that_finds_a_message_deleted_takes_back_the_note_saved_from_it(  # noqa: F811
        learning, monkeypatch):
    """The issue: member 11's message is deleted in Discord while the summary request signs in, and its event never
    reaches Fri. Reading the batch again, the pass left it out, but queued nothing, so the note Fri learned from it
    earlier stayed in Gene. Read again by its ID, Discord says it's gone: it's withdrawn under the server, and Gene
    drops the note."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        await every_capability(agent)
        noted(store, first)
        assert notes(store) == [TRANCE]
        channel, sent = agent.fri.get_channel(1), recorded(agent)
        signing.append(lambda: channel.rows.remove(next(row for row in channel.rows if str(row.id) == first)))
        await agent.learn()
        await drained(agent)
        assert sent_ids(asked) == [[second]]  # Left out, as before.
        assert [record['id'] for record in finishes[-1]['records']] == [second] and not finishes[-1]['error']
        assert [(payload['guild_id'], payload['message_ids'], payload.get('deleted')) for payload in sent(SAVED)] == [
            ('123', [first], True)]
        assert notes(store) == []
    asyncio.run(scenario())


@pytest.mark.parametrize('how', ['out of the window', *UNCONFIRMED])
def test_a_learning_pass_withdraws_nothing_discord_doesnt_say_was_deleted(learning, monkeypatch, how):  # noqa: F811
    """Missing from the history read again, but not a 404 by its ID: still there (the window moved), refused, failing,
    or timing out. It's left out as before, the pass completes, and nothing is withdrawn: the note stays."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        await every_capability(agent)
        noted(store, first)
        channel, sent = agent.fri.get_channel(1), recorded(agent)
        http, logs = channel._state.http, channel._state.http.logs_from

        async def reading(*args, **kwargs):
            return [row for row in await logs(*args, **kwargs) if row['id'] != first]

        async def by_id(channel_id, message_id):
            answer(how)

        def hide():
            http.logs_from = reading
            if how != 'out of the window':
                http.get_message = by_id

        signing.append(hide)
        await agent.learn()
        await drained(agent)
        assert sent_ids(asked) == [[second]]
        assert [record['id'] for record in finishes[-1]['records']] == [second] and not finishes[-1]['error']
        assert not sent(SAVED) and first not in agent.saved_turns.pending
        assert notes(store) == [TRANCE]
    asyncio.run(scenario())


def test_a_learning_pass_with_nothing_missing_reads_nothing_by_id(learning, monkeypatch):  # noqa: F811
    """The ordinary pass: everything read again as it was, so its request is main's and nothing is read by ID."""
    async def scenario():
        agent, store, (first, second), finishes, asked, signing = batch(learning, monkeypatch)
        http, reads = agent.fri.get_channel(1)._state.http, []
        original = http.get_message

        async def by_id(channel_id, message_id):
            reads.append(message_id)
            return await original(channel_id, message_id)

        http.get_message = by_id
        await agent.learn()
        assert sent_ids(asked) == [[first, second]] and reads == []
        assert [record['id'] for record in finishes[-1]['records']] == [first, second]
    asyncio.run(scenario())


# R1 on an archive pass (fri_explore).

def test_an_archive_pass_that_finds_a_row_deleted_takes_it_back(learning, monkeypatch):  # noqa: F811
    """The issue's repro on the exploration path: member 11's 120-day-old message is deleted in Discord while the
    archive summary signs in, with no event. Its by-ID read is a 404: left out as before, and now taken back under the
    server too."""
    found = taken(monkeypatch)
    _, summarized, old = explorer(learning, 'its row, in Discord alone, while the summary signs in')
    assert summarized == [] and found == [(1, [int(old)], 123)]


@pytest.mark.parametrize('moved', ['its row, refused by its ID, while the summary signs in',
                                   'its row, through Gene alone, while the summary signs in'])
def test_an_archive_pass_takes_back_nothing_discord_doesnt_say_was_deleted(learning, monkeypatch, moved):  # noqa: F811
    """Refused by its ID (the pass stops, as any failed read stops it), or withdrawn through Gene alone with Discord
    still showing it: nothing is sent, and nothing is taken back here."""
    found = taken(monkeypatch)
    _, summarized, _ = explorer(learning, moved)
    assert summarized == [] and found == []


# R1 on a read's recheck (Actions.reads_unchanged).

def refusing(channel, message, how):
    fetch = channel.fetch_message.side_effect

    async def by_id(mid):
        if mid == message.id:
            answer(how)
        return await fetch(mid)

    channel.fetch_message.side_effect = by_id


@pytest.mark.parametrize('change', ['deleted', 'unpinned', *UNCONFIRMED])
def test_a_pins_recheck_takes_back_only_a_post_discord_says_is_deleted(monkeypatch, tmp_path, change):
    """A deleted pinned post behaves the same, the issue says. Off the pins list, a post may only be unpinned: read by
    its ID, a 404 is taken back under the read's server; still there, refused, or failing, nothing is. The recheck
    fails either way, as before."""
    async def scenario():
        found = taken(monkeypatch)
        fri, agent, channel, rows, post, pin, pins, calls = pinned(monkeypatch, tmp_path)
        rules = pin('rules: be nice')
        pin('server ip is play.example.net')
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'pins'})
            pins.remove(rules.id)
            if change == 'deleted':
                del rows[rules.id]
            elif change != 'unpinned':
                refusing(channel, rules, change)
            assert not await actions.reads_unchanged()
            assert found == ([(1, [rules.id], 123)] if change == 'deleted' else [])
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['deleted', 'none', *UNCONFIRMED])
def test_a_reads_recheck_takes_back_only_a_post_discord_says_is_deleted(monkeypatch, tmp_path, change):
    """The same for a read's window: gone from the history read again, a 404 by its ID is taken back; refused or
    failing, nothing is. Unchanged, nothing is read by ID."""
    async def scenario():
        found = taken(monkeypatch)
        fri, agent, channel, rows, post = read_harness(monkeypatch, tmp_path)
        plan = post('my plan for friday', age=90)
        post('nice', user=50, age=30)
        actions = Actions(agent, 20, 999, False)
        actions.event = SimpleNamespace(id=1, channel=SimpleNamespace(id=999))
        try:
            await actions.execute({'kind': 'read_recent', 'channel_id': '1', 'window': 'recent'})
            if change != 'none':
                del rows[plan.id]
            if change in UNCONFIRMED:
                refusing(channel, plan, change)
            assert await actions.reads_unchanged() is (change == 'none')
            assert found == ([(1, [plan.id], 123)] if change == 'deleted' else [])
            if change == 'none':
                channel.fetch_message.assert_not_awaited()
        finally:
            await fri.close()
    asyncio.run(scenario())


# R1 on an archive search (HistorySearch).

@pytest.mark.parametrize('change', ['deleted', 'refused'])
def test_an_archive_search_takes_back_a_result_discord_says_is_deleted(monkeypatch, tmp_path, change):
    """Discord's index can still list a message after it's deleted. Read by its ID, a 404 leaves it out, as before, and
    now takes it back under the server; a refusal takes back nothing."""
    async def scenario():
        found = taken(monkeypatch)
        fri, agent, actions, channels, rows, messages = setup(monkeypatch, tmp_path)
        channel, gone = channels[1], int(rows[0]['id'])
        fetch = channel.fetch_message.side_effect

        async def by_id(mid):
            if mid == gone and change == 'deleted':
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
            if mid == gone:
                answer(change)
            return await fetch(mid)

        channel.fetch_message.side_effect = by_id
        try:
            result = await actions.execute(query())
            assert str(gone) not in str(result.get('messages'))
            assert found == ([(1, [gone], 123)] if change == 'deleted' else [])
        finally:
            await fri.close()
    asyncio.run(scenario())


# L1: a hold past its time.

@pytest.mark.parametrize('check', ['withholding', 'withheld', 'status'])
@pytest.mark.parametrize('horizon', ['a note’s 90 days', 'a week, from a controller without withdrawal-sources'])
def test_a_hold_past_its_time_stops_holding_back_as_soon_as_anything_asks(monkeypatch, tmp_path, check, horizon):
    """The issue: a by-ID hold restored from disk, then past its deadline, stayed pending when withholding() or
    status() asked, and summaries stayed paused, until another withdrawal, a saved turn or a restart pruned it. Each
    check prunes first now: never before the deadline, at once after it, for the horizon the controller offers now."""
    async def scenario():
        w = await world(monkeypatch, tmp_path)
        needs_sources(w)
        try:
            old = old_message()
            only_queued(monkeypatch)
            failing(w, SAVED)
            await delete(w, old, guild=123)
            w.agent.saved_turns = turns = SavedTurns(w.agent)  # Restarted: held from disk.
            assert turns.pending[str(old.id)].get('hold') is True
            kept = NOTED_SECONDS
            if horizon.startswith('a week'):
                w.fri.capabilities = frozenset(w.fri.capabilities)-{SOURCES}
                kept = KEPT_SECONDS

            def holding():
                if check == 'withholding':
                    return turns.withholding()
                if check == 'withheld':
                    return turns.withheld([old.id]) == {str(old.id)}
                return turns.status()['pending'] == 1

            module_clock(monkeypatch, kept-DAY)
            assert holding()  # Never shorter.
            module_clock(monkeypatch, 2*DAY)
            assert not holding()
            assert str(old.id) not in turns.pending
        finally:
            await w.fri.close()
    asyncio.run(scenario())
