"""What a /fri panel reply, and My memory, hold back for a message taken back that saved context drew on (fri #332,
fri #324 item 2), against the real controller.

A panel reply is held back against the messages its requests carried. Those were the saved exchanges' own messages,
not what each saved reply drew on, nor the member summary's sources, so deleting a message a saved reply quoted, or one
the summary was built from, while the reply waited, showed and saved it. Now what the saved context drew on counts too:
exactly, for an exchange this Fri saved itself; and where that can't be established (an exchange saved before it
started), any message taken back while the turn ran holds the reply back. The summary's sources are the member's own
messages from before it was made, so one sent since, or by someone else, can't be one of them. No model request reads
any of it; saved context whose dependency is withheld leaves a request as its own message would.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_endings import source
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from compat_tests.test_fri_panel_delivery import edits
from fri.fri_chat import ChatResult

MARKER = 'synthetic-quoted-marker'
REPLY = 'ash said '+MARKER


def quoting(store, fri, quoted, tracked):
    """Member 20's saved exchange in the panel's conversation, whose reply quoted `quoted` (its turn's drew_on): saved
    by this Fri (tracked), or before it started, so all it knows is the exchange."""
    said = source(age=600, channel='456')
    store.save_chat('123', 'private:456', '20', 0, 'what did ash say', REPLY, 'model', 'high', source=said,
                    provenance=[], drew_on=[quoted])
    if tracked:
        payload = {'guild_id': '123', 'channel_id': '456', 'conversation_id': 'private:456', 'user_id': '20',
                   'revision': 0, 'source': said, 'drew_on': [quoted]}
        fri.agent.saved_turns.kept(payload, {}, (time.time(), time.monotonic()))
    return said


DELETED = {'the quoted message': 'quoted', 'an older message it never quoted': 'older',
           'a message sent since': 'newer', 'nothing': None}


@pytest.mark.parametrize('tracked', [True, False], ids=['saved by this Fri', 'saved before it started'])
@pytest.mark.parametrize('deleted', list(DELETED))
def test_332_a_panel_reply_is_held_to_what_its_saved_replies_drew_on(monkeypatch, tmp_path, tracked, deleted):
    """The issue's first repro: member 20's saved exchange B quotes a marker from message A (drew_on=[A]), and their
    /fri panel turn reads B and repeats it. While the reply waits for the panel's lock, A is deleted and its withdrawal
    times out: B's own message was all the hold-back knew, so the marker showed and was saved. Now it's held back.
    For an exchange this Fri saved, it knows what B drew on, so a message B never quoted changes nothing; for one saved
    before it started it can't tell, so any message taken back while the turn ran holds the reply back."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        interaction = panel(fri)
        quoted, older = source(age=1200, channel='1'), source(age=1100, channel='1')
        quoting(store, fri, quoted['id'], tracked)
        newer = source(age=0, channel='1')
        gone = {'quoted': quoted, 'older': older, 'newer': newer, None: None}[DELETED[deleted]]

        async def respond(text, context, ready):
            await ready()
            assert MARKER in str(context.get('turns'))  # The request carried the saved exchange.
            return ChatResult(status='completed', reply=REPLY)
        model(fri, tmp_path, respond)
        api, saved = fri.api, []
        lock = fri.private_panels.locks.setdefault((123, 20), asyncio.Lock())
        taken = []

        async def holder():
            while not getattr(lock, '_waiters', None):
                await asyncio.sleep(0)
            if gone:
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123, message_id=int(gone['id'])))
            lock.release()

        async def recording(method, path, payload=None):
            if path.startswith('/chat/turn/withdraw'):
                raise TimeoutError()  # Gene doesn't take the withdrawal: B's reply still holds the marker.
            if path == '/chat/turn':
                saved.append(payload)
            answer = await api(method, path, payload)
            if path == '/chat/turn/check' and not taken:
                taken.append(path)
                await lock.acquire()
                asyncio.get_running_loop().create_task(holder())
            return answer
        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'what did ash say again')
            assert taken
            held = DELETED[deleted] == 'quoted' or (DELETED[deleted] is not None and not tracked)
            if held:
                assert edits(interaction, REPLY) == 0 and not saved and shown(interaction) == skipped()
            else:
                assert REPLY in shown(interaction) and saved
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_332_a_deletion_where_fri_saves_no_exchange_still_holds_a_reply_that_drew_on_it(monkeypatch, tmp_path):
    """A message a saved reply quoted, linked from a channel Fri doesn't chat in, so Fri looked at it to answer, deleted
    while the reply waits: nothing is queued or marked for it, only its deletion is known here (see Agent.unsend).
    That holds the reply back too."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        interaction = panel(fri)
        quoted = source(age=1200, channel='1')
        quoting(store, fri, quoted['id'], tracked=True)

        async def respond(text, context, ready):
            await ready()
            return ChatResult(status='completed', reply=REPLY)
        model(fri, tmp_path, respond)
        api, saved, taken = fri.api, [], []
        lock = fri.private_panels.locks.setdefault((123, 20), asyncio.Lock())

        async def holder():
            while not getattr(lock, '_waiters', None):
                await asyncio.sleep(0)
            agent.saved_in = lambda event: None  # A channel Fri doesn't chat in.
            agent.looked[int(quoted['id'])] = (time.monotonic(), False)  # Looked at to answer B.
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=77, guild_id=123, message_id=int(quoted['id'])))
            assert int(quoted['id']) in agent.unsent
            assert quoted['id'] not in agent.saved_turns.marked
            lock.release()

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            answer = await api(method, path, payload)
            if path == '/chat/turn/check' and not taken:
                taken.append(path)
                await lock.acquire()
                asyncio.get_running_loop().create_task(holder())
            return answer
        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'what did ash say again')
            assert taken and edits(interaction, REPLY) == 0 and not saved
        finally:
            await fri.close()
    asyncio.run(scenario())


async def unrelated_deletion_while_waiting(fri, agent, interaction, tmp_path, before=None):
    """Member 20's panel turn, with an unrelated message deleted while its reply waits for the panel's lock, after
    `before()` (if given) runs ahead of the turn. Returns whether the reply showed and was saved."""
    unrelated = source(age=1100, channel='1')

    async def respond(text, context, ready):
        await ready()
        return ChatResult(status='completed', reply='nothing to repeat')
    model(fri, tmp_path, respond)
    if before:
        await before()
    api, saved, taken = fri.api, [], []
    lock = fri.private_panels.locks.setdefault((123, 20), asyncio.Lock())

    async def holder():
        while not getattr(lock, '_waiters', None):
            await asyncio.sleep(0)
        await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123, message_id=int(unrelated['id'])))
        lock.release()

    async def recording(method, path, payload=None):
        if path.startswith('/chat/turn/withdraw'):
            raise TimeoutError()
        if path == '/chat/turn':
            saved.append(payload)
        answer = await api(method, path, payload)
        if path == '/chat/turn/check' and not taken:
            taken.append(path)
            await lock.acquire()
            asyncio.get_running_loop().create_task(holder())
        return answer
    fri.api = recording
    await fri.chat_interaction(interaction, 'what did ash say again')
    return bool(taken) and 'nothing to repeat' in shown(interaction) and bool(saved)


def test_332_a_saved_exchange_with_no_reply_drew_on_nothing(monkeypatch, tmp_path):
    """Saved before Fri started, but with no reply (a reaction, say): it drew on nothing, so a message taken back
    while the turn runs, which it can't have read, doesn't hold the reply back."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        store.save_chat('123', 'private:456', '20', 0, 'nice one', '', 'model', 'high', provenance=[],
                        source=source(age=60, channel='456'), response_kind='react', reaction='\U0001F44D')
        try:
            assert await unrelated_deletion_while_waiting(fri, agent, panel(fri), tmp_path)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_332_saved_context_withheld_before_the_request_counts_for_nothing(monkeypatch, tmp_path):
    """A saved reply this Fri can't place, its own message deleted before the turn: it stays out of the request, so
    what it drew on can't hold the reply back, and an unrelated message taken back while the turn runs doesn't."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        said = quoting(store, fri, source(age=1200, channel='1')['id'], tracked=False)

        async def deleted():
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=456, guild_id=123, message_id=int(said['id'])))
        try:
            assert await unrelated_deletion_while_waiting(fri, agent, panel(fri), tmp_path, before=deleted)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_332_a_reply_of_fris_a_saved_reply_drew_on_counts_with_what_it_drew_on(monkeypatch, tmp_path):
    """Transitively: B drew on Fri's own reply R (an echo it repeated), and R drew on A. Deleting A holds a reply
    that read B back, as it would one that read R."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        quoted, echoed = source(age=2400, channel='1'), source(age=1800, channel='1')
        turns = agent.saved_turns
        # R: Fri's reply in #1, which drew on A, saved by this Fri.
        turns.kept({'guild_id': '123', 'channel_id': '1', 'conversation_id': 'public:1', 'user_id': '30',
                    'revision': 0, 'source': source(age=1900, channel='1', user='30'), 'drew_on': [quoted['id']],
                    'reply_message_id': echoed['id']}, {}, (time.time(), time.monotonic()))
        said = quoting(store, fri, echoed['id'], tracked=True)
        context = {'turns': [{'source': said, 'reply': REPLY, 'burst': []}]}
        depends, unknown = turns.dependencies(context)
        assert {quoted['id'], echoed['id']} <= depends and not unknown
        await fri.close()
    asyncio.run(scenario())


def test_332_a_saved_reply_whose_dependency_is_withheld_stays_out_of_the_request(monkeypatch, tmp_path):
    """Taken back before the turn, its withdrawal still on its way to Gene: B, which drew on A, stays out of the
    request, as an exchange holding A itself would (fri #215), and the reply that reads the rest shows."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        interaction = panel(fri)
        quoted = source(age=1200, channel='1')
        quoting(store, fri, quoted['id'], tracked=True)
        api, carried = fri.api, []

        async def down(method, path, payload=None):
            if path.startswith('/chat/turn/withdraw'):
                raise TimeoutError()
            return await api(method, path, payload)
        fri.api = down
        await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123, message_id=int(quoted['id'])))

        async def respond(text, context, ready):
            await ready()
            carried.append(str(context.get('turns')))
            return ChatResult(status='completed', reply='nothing to repeat')
        model(fri, tmp_path, respond)
        try:
            await fri.chat_interaction(interaction, 'what did ash say again')
            assert carried and MARKER not in carried[0]
            assert 'nothing to repeat' in shown(interaction)
        finally:
            await fri.close()
    asyncio.run(scenario())


# The member summary: repro 2, and fri #324 item 2.

def summary_from(w, monkeypatch):
    """Member 20's summary, built from their exchange in #general (see test_fri_panel_fences_3). Returns the message
    under it."""
    from compat_tests.test_fri_member_profiles import GENERAL
    from compat_tests.test_fri_member_summaries import ASH, settled, summarized, summarizer
    from fri.fri_member_summary import build
    posted = w.message(GENERAL, 'ash says the raid is at nine')
    said = {'kind': 'discord_message', 'id': str(posted.id), 'user_id': '20', 'display_name': 'friend',
            'channel_id': str(GENERAL), 'guild_id': '123'}
    w.store.save_chat('123', 'public:1', '20', 0, 'ash says the raid is at nine', 'ok', 'm', 'high', source=said,
                      provenance=[ASH], audience='server')
    event = w.message(GENERAL, 'fri anyway')

    async def go():
        await w.say(GENERAL, 'fri anyway', event=event)
        settled(w, 13*3600)
        summarizer(monkeypatch, w)
        assert await build(w.fri.agent) == {'kept': True} and summarized(w)
    return event.id, go


def newer_exchanges(w):
    """Six newer exchanges of member 20's in #general: the one under the summary leaves the six Gene keeps."""
    from compat_tests.test_fri_member_profiles import GENERAL
    for n in range(6):
        posted = w.message(GENERAL, f'line {n}')
        w.store.save_chat('123', 'public:1', '20', 0, f'line {n}', 'ok', 'm', 'high', audience='server', provenance=[],
                          source={'kind': 'discord_message', 'id': str(posted.id), 'user_id': '20',
                                  'display_name': 'friend', 'channel_id': str(GENERAL), 'guild_id': '123'})


TAKEN = {'its own source': 'under', 'a message of someone else, before it': 'theirs',
         'a message of theirs sent since': 'since', 'nothing': None}


@pytest.mark.parametrize('taken', list(TAKEN))
def test_324_my_memory_holds_the_summary_while_a_pending_withdrawal_may_be_its_source(monkeypatch, tmp_path, taken):
    """fri #324 item 2: member 20's summary, six newer exchanges since, so its source has left the six Gene keeps, and
    a deletion of that source queued here while its delivery is blocked. My memory screened the summary against the
    six exchanges only, so it showed. Now a pending withdrawal holds it back unless it can be shown unrelated: the
    message was sent after the summary was made, or its author is known and someone else."""
    async def scenario():
        from compat_tests.test_fri_member_profiles import GENERAL, world
        from compat_tests.test_fri_member_summaries import SUMMARY
        from compat_tests.test_fri_panel_fences_2 import DM_SCOPE, dm_panel, listed
        from fri.fri_memory import profiled
        from fri.fri_memory_panel import show
        w = world(monkeypatch, tmp_path)
        try:
            under, go = summary_from(w, monkeypatch)
            await go()
            newer_exchanges(w)
            monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
            turns = w.fri.agent.saved_turns
            turns.kick = lambda: None  # Its withdrawal stays queued here: Gene hasn't taken it yet.
            theirs = w.message(GENERAL, 'something else entirely')
            if TAKEN[taken] == 'under':
                turns.withdraw([under], 123, deleted=True)
            elif TAKEN[taken] == 'theirs':
                turns.withdraw([under-1], 123, deleted=True, authors={under-1: 30})  # Sent before it, by member 30.
            elif TAKEN[taken] == 'since':
                turns.withdraw([theirs.id], 123, deleted=True)
            interaction = dm_panel(w.fri)
            await show(w.fri, interaction, profiled(w.fri, dict(DM_SCOPE)))
            assert (listed(interaction, SUMMARY) > 0) is (TAKEN[taken] != 'under')
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('deleted', ['its source, while the reply waits', 'its source, before the turn',
                                     "someone else's message, before the turn", 'nothing'])
def test_332_a_panel_reply_drawing_on_the_summary_is_held_to_its_sources(monkeypatch, tmp_path, deleted):
    """The issue's second repro: member 20's /fri panel turn reads their summary, built from message A. A is deleted
    during Gene's last check, and its withdrawal stays queued here: the reply had no source of the summary to hold it
    to, so it showed and was saved. Now it's held back. Deleted before the turn, the summary stays out of the request
    instead, and the reply shows; a deletion of someone else's message, its author known, leaves the summary in."""
    async def scenario():
        from compat_tests.test_fri_member_profiles import GENERAL, world
        from compat_tests.test_fri_member_summaries import SUMMARY
        w = world(monkeypatch, tmp_path)
        try:
            under, go = summary_from(w, monkeypatch)
            await go()
            newer_exchanges(w)
            turns = w.fri.agent.saved_turns
            turns.kick = lambda: None
            interaction = panel(w.fri)
            before = deleted.endswith('before the turn')
            if deleted == 'its source, before the turn':
                await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=GENERAL, guild_id=123, message_id=under))
            elif deleted == "someone else's message, before the turn":
                turns.withdraw([under-1], 123, deleted=True, authors={under-1: 30})
            responded, carried = [], []

            async def respond(text, context, ready):
                await ready()
                carried.append(context.get('member_summary'))
                responded.append(text)
                return ChatResult(status='completed', reply='you plan raids, '+MARKER)
            model(w.fri, tmp_path, respond)
            api, saved, deleting = w.fri.api, [], []

            async def recording(method, path, payload=None):
                if path == '/chat/turn':
                    saved.append(payload)
                answer = await api(method, path, payload)
                # Gene's last check, after the model answered.
                if (path == '/chat/turn/check' and deleted == 'its source, while the reply waits' and responded
                        and not deleting):
                    deleting.append(path)
                    await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=GENERAL, guild_id=123,
                                                                      message_id=under))
                return answer
            w.fri.api = recording
            await w.fri.chat_interaction(interaction, 'remind me what we planned')
            assert carried
            # The request carried the summary, but where its source was taken back before.
            assert (SUMMARY in str(carried[0])) is (deleted != 'its source, before the turn')
            if deleted == 'its source, while the reply waits':
                assert deleting and MARKER not in shown(interaction) and not saved
            else:
                assert MARKER in shown(interaction) and saved and (before or deleted == 'nothing')
        finally:
            await w.fri.close()
    asyncio.run(scenario())


# Sol round 1 of #382: S1 and S2, both over-withholding.

@pytest.mark.parametrize('deleted', ['a message neither drew on', 'nothing'])
def test_382_r1_s1_after_an_ordinary_panel_exchange_the_next_reply_shows(monkeypatch, tmp_path, deleted):
    """Sol round 1's S1: member 20's ordinary /fri panel exchange, then their next panel turn, and while its reply waits
    a message neither drew on is deleted. The first exchange's source, an interaction, never entered what this Fri knows
    its replies drew on, and its save named nothing it drew on, so it read as one saved before this Fri started: the
    reply was held back. Main shows it, and so does this now."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        unrelated = source(age=1200, channel='1')
        carried = []

        async def respond(text, context, ready):
            await ready()
            carried.append(str(context.get('turns')))
            return ChatResult(status='completed', reply='first answer' if len(carried) == 1 else 'second answer')
        model(fri, tmp_path, respond)
        first = panel(fri)
        await fri.chat_interaction(first, 'hey')
        assert 'first answer' in shown(first)
        second = panel(fri)
        second.id = first.id+1
        api, saved, taken = fri.api, [], []
        lock = fri.private_panels.locks.setdefault((123, 20), asyncio.Lock())

        async def holder():
            while not getattr(lock, '_waiters', None):
                await asyncio.sleep(0)
            if deleted != 'nothing':
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123,
                                                                message_id=int(unrelated['id'])))
                assert unrelated['id'] in agent.saved_turns.marked
            lock.release()

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            answer = await api(method, path, payload)
            if path == '/chat/turn/check' and not taken:
                taken.append(path)
                await lock.acquire()
                asyncio.get_running_loop().create_task(holder())
            return answer
        fri.api = recording
        try:
            await fri.chat_interaction(second, 'and now')
            assert taken and 'first answer' in carried[-1]  # The request carried the first exchange.
            assert 'second answer' in shown(second) and saved
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('derived', [True, False], ids=['a model reply', 'one Fri answers itself'])
def test_382_r1_s1_a_turn_that_carried_nothing_else_names_an_empty_list(monkeypatch, derived):
    """S1's second cause: a turn whose requests carried nothing besides its own message, and one Fri answers itself (a
    usage report, a control), named nothing it drew on. To a controller offering withdrawal sources it names an empty
    list now (the controller's own default), so the reply reads as drawing on nothing. An older controller gets
    nothing, as before."""
    from compat_tests.test_fri_runtime import configured
    from fri.fri_context import ChatSource
    from fri.fri_turn_withdrawal import SOURCES
    fri = configured(monkeypatch)
    said = source(age=60)
    scope = {'guild_id': '123', 'channel_id': '456', 'conversation_id': 'private:456', 'user_id': '20'}
    context = {'revision': 0, 'model': 'gpt-6.1-sol', 'effort': 'high', 'turns': [], 'recent': [],
               'fence': {'turn': {'source': said}}}
    for capabilities, named in ((frozenset({SOURCES}), {'drew_on': []}), (frozenset(), {})):
        fri.capabilities = capabilities
        turn = fri.stage_turn(scope, ChatSource(**said), 'hey', context, ChatResult(reply='yo', status='completed'),
                              **({} if derived else {'model': 'local-control', 'derived': False})).turn
        assert {key: turn[key] for key in turn if key == 'drew_on'} == named


S2 = ["someone else's message", "a reply of Fri's that carried it", "someone else's exchange this Fri saved",
      "someone else's exchange, queued before a restart", 'its own source',
      "someone else's message, and they said forget me"]


@pytest.mark.parametrize('taken', S2)
def test_382_r1_s2_a_withdrawal_the_controller_took_keeps_its_author_for_my_memory(monkeypatch, tmp_path, taken):
    """Sol round 1's S2: member 20's summary, and an older message of member 30's withdrawn, its author known. Once the
    controller took the withdrawal, it left the queue and its author with it, while its mark stays an hour, so My
    memory hid the summary for that hour; main shows it. The author stays with the mark now, and Fri's own replies that
    carried a deleted message are marked as Fri's. The summary's own source still hides it, and so does one whose
    author said "forget me" since: what Fri knew of them goes, so it can't be ruled out."""
    async def scenario():
        from compat_tests.test_fri_member_summaries import SUMMARY
        from compat_tests.test_fri_panel_fences_2 import DM_SCOPE, dm_panel, listed
        from fri.fri_memory import profiled
        from fri.fri_memory_panel import show
        w = taken_by_the_controller(monkeypatch, tmp_path, taken)
        try:
            under, gone = await w.taken()
            turns = w.fri.agent.saved_turns
            if taken.endswith('forget me'):
                w.fri.agent.forget('30', erased=True)
                assert str(under-1) in turns.marked and str(under-1) not in turns.marked_by
            monkeypatch.setattr('fri.fri_memory_panel.allowed', AsyncMock(return_value=True))
            interaction = dm_panel(w.fri)
            await show(w.fri, interaction, profiled(w.fri, dict(DM_SCOPE)))
            assert (listed(interaction, SUMMARY) > 0) is (taken in S2[:4])
            turns.prune(time.time()+3601)  # Its hour past: the author goes with the mark.
            assert not {str(i) for i in gone} & (set(turns.marked) | set(turns.marked_by))
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def taken_by_the_controller(monkeypatch, tmp_path, taken):
    """Member 20's summary, six newer exchanges, and a withdrawal the controller has taken: of the summary's source, or
    of an older message of member 30's (and with it a reply of Fri's that carried it, for that case)."""
    from compat_tests.test_fri_member_profiles import world
    w = world(monkeypatch, tmp_path)
    under, go = summary_from(w, monkeypatch)

    async def done():
        await go()
        newer_exchanges(w)
        turns = w.fri.agent.saved_turns
        if taken == "a reply of Fri's that carried it":  # In view, its turn having carried member 30's message.
            w.fri.agent.drawn[str(under+1)] = (time.time(), frozenset({str(under-1)}))
        if taken == 'its own source':
            gone = [under]
            turns.withdraw(gone, 123, deleted=True, authors={under: 20})
        elif 'exchange' in taken:
            # An exchange of member 30's this Fri saved, its author named by it, no one saying so. Its withdrawal names
            # them on disk too (one by ID alone never does: see SavedTurns.lasting): for a restart, it's queued when
            # Fri stops.
            from compat_tests.test_fri_member_profiles import GENERAL
            gone = [under-1]
            said = {'kind': 'discord_message', 'id': str(under-1), 'user_id': '30', 'channel_id': str(GENERAL),
                    'guild_id': '123', 'display_name': 'other'}
            turns.kept({'guild_id': '123', 'channel_id': str(GENERAL), 'conversation_id': f'public:{GENERAL}',
                        'user_id': '30', 'revision': 0, 'source': said}, {}, (time.time(), time.monotonic()))
            if taken.endswith('before a restart'):
                turns.kick = lambda: None
            turns.withdraw(gone, 123, deleted=True)
        else:
            gone = [under-1]
            turns.withdraw(gone, 123, deleted=True, authors={under-1: 30})
        if taken.endswith('before a restart'):  # Fri starts again, and sends it.
            from fri.fri_turn_withdrawal import SavedTurns
            w.fri.agent.saved_turns = turns = SavedTurns(w.fri.agent)
            assert turns.pending
            turns.kick()
        await asyncio.gather(*list(turns.tasks))
        assert not turns.pending and all(str(i) in turns.marked for i in gone)  # Taken, still marked.
        if taken == "a reply of Fri's that carried it":
            assert str(under+1) in turns.marked
        return under, gone
    w.taken = done
    return w


def test_382_r1_s2_a_withdrawal_the_controller_took_of_someone_elses_message_leaves_the_summary_in_a_request(
        monkeypatch, tmp_path):
    """S2 for the panel: the same withdrawal, taken before member 20's panel turn, took their summary out of its
    request for the hour; now it stays."""
    async def scenario():
        from compat_tests.test_fri_member_summaries import SUMMARY
        w = taken_by_the_controller(monkeypatch, tmp_path, "someone else's message")
        try:
            await w.taken()
            carried = []

            async def respond(text, context, ready):
                await ready()
                carried.append(context.get('member_summary'))
                return ChatResult(status='completed', reply='you plan raids, '+MARKER)
            model(w.fri, tmp_path, respond)
            interaction = panel(w.fri)
            await w.fri.chat_interaction(interaction, 'remind me what we planned')
            assert carried and SUMMARY in str(carried[0]) and MARKER in shown(interaction)
        finally:
            await w.fri.close()
    asyncio.run(scenario())
