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


@pytest.mark.parametrize('deleted', [True, False], ids=['its source deleted', 'nothing'])
def test_332_a_panel_reply_drawing_on_the_summary_is_held_to_its_sources(monkeypatch, tmp_path, deleted):
    """The issue's second repro: member 20's /fri panel turn reads their summary, built from message A. A is deleted
    during Gene's last check, and its withdrawal stays queued here: the reply had no source of the summary to hold it
    to, so it showed and was saved. Now it's held back."""
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

            responded = []

            async def respond(text, context, ready):
                await ready()
                assert SUMMARY in str(context.get('member_summary'))  # The request carried the summary.
                responded.append(text)
                return ChatResult(status='completed', reply='you plan raids, '+MARKER)
            model(w.fri, tmp_path, respond)
            api, saved, deleting = w.fri.api, [], []

            async def recording(method, path, payload=None):
                if path == '/chat/turn':
                    saved.append(payload)
                answer = await api(method, path, payload)
                # Gene's last check, after the model answered.
                if path == '/chat/turn/check' and deleted and responded and not deleting:
                    deleting.append(path)
                    await w.fri.on_raw_message_delete(SimpleNamespace(channel_id=GENERAL, guild_id=123,
                                                                      message_id=under))
                return answer
            w.fri.api = recording
            await w.fri.chat_interaction(interaction, 'remind me what we planned')
            if deleted:
                assert deleting and MARKER not in shown(interaction) and not saved
            else:
                assert MARKER in shown(interaction) and saved
        finally:
            await w.fri.close()
    asyncio.run(scenario())
