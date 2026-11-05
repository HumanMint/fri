"""Saved replies' dependencies past #382 (fri #383), against the real controller.

1. Unknown ancestry under a known descendant: B quoted A before this Fri started, so what B drew on isn't known. C,
   saved by this Fri, drew on B, and B left the six exchanges Gene keeps. A turn reading C knew C, and C named only B,
   so A's deletion while it ran went unseen. C now carries that it drew on what can't be told.
2. A saved reply quoting the member's summary, while a message taken back may be one of its sources: the summary left
   the request, and the reply that quoted it stays out of it too.
3. A DM's and a channel's reply are held to what their saved context drew on, as a /fri panel reply is (fri #382).
No model input changes but what's withheld.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_endings import source
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from compat_tests.test_fri_panel_delivery import edits
from compat_tests.test_fri_panel_dependency_holds import (
    MARKER,
    REPLY,
    newer_exchanges,
    quoting,
    summary_from,
)
from compat_tests.test_fri_rerun_fence import direct
from fri.fri_chat import ChatResult


async def held_while_waiting(fri, agent, interaction, text, gone):
    """Member 20's panel turn, with `gone` (a message, or None) deleted while its reply waits for the panel's lock,
    its withdrawal timing out. Returns what was saved."""
    api, saved, taken = fri.api, [], []
    lock = fri.private_panels.locks.setdefault((123, 20), asyncio.Lock())

    async def holder():
        while not getattr(lock, '_waiters', None):
            await asyncio.sleep(0)
        if gone:
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123, message_id=int(gone['id'])))
        lock.release()

    async def recording(method, path, payload=None):
        if path.startswith('/chat/turn/withdraw'):
            raise TimeoutError()  # Gene doesn't take it: what quoted it still holds the words.
        if path == '/chat/turn':
            saved.append(payload)
        answer = await api(method, path, payload)
        if path == '/chat/turn/check' and not taken:
            taken.append(path)
            await lock.acquire()
            asyncio.get_running_loop().create_task(holder())
        return answer
    fri.api = recording
    await fri.chat_interaction(interaction, text)
    fri.api = api
    assert taken
    return saved


# 1. Unknown ancestry.

@pytest.mark.parametrize('gone', ['the quoted message', 'nothing'])
def test_a_reply_drawing_on_one_saved_before_fri_started_carries_that_it_cant_tell(monkeypatch, tmp_path, gone):
    """The issue's repro: B, saved before this Fri started, quoted A. Member 20's panel turn reads B and repeats it, and
    this Fri saves that reply, C, drawing on B. B leaves the six exchanges Gene keeps. The next panel turn reads C, and A
    is deleted while its reply waits, its withdrawal still pending: held back, as for B itself."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        quoted = source(age=1200, channel='1')
        said = quoting(store, fri, quoted['id'], tracked=False)
        carried = []

        async def respond(text, context, ready):
            await ready()
            carried.append(str(context.get('turns')))
            return ChatResult(status='completed', reply=REPLY)
        model(fri, tmp_path, respond)
        first = panel(fri)
        await fri.chat_interaction(first, 'what did ash say')
        assert REPLY in shown(first)
        with store.connection() as db:  # B leaves the six exchanges Gene keeps.
            db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (said['id'],))
        second = panel(fri)
        second.id = first.id+1
        try:
            saved = await held_while_waiting(fri, agent, second, 'say that again', quoted if gone != 'nothing' else None)
            assert MARKER in carried[-1] and said['id'] not in carried[-1]  # It read C, not B.
            if gone == 'nothing':
                assert REPLY in shown(second) and saved
            else:
                assert edits(second, REPLY) == 0 and not saved and shown(second) == skipped()
        finally:
            await fri.close()
    asyncio.run(scenario())


# 2. A saved reply quoting a held summary.

@pytest.mark.parametrize('queued', [True, False], ids=['its source queued', 'nothing'])
def test_a_saved_reply_quoting_the_summary_leaves_the_request_with_it(monkeypatch, tmp_path, queued):
    """The issue's repro: member 20's summary S, built from A; A leaves the six exchanges; then B, a saved reply in the
    same channel that quoted S. A's withdrawal is queued here, and member 20 asks a follow-up: the summary leaves the
    request, and so does B."""
    async def scenario():
        from compat_tests.test_fri_member_profiles import GENERAL, world
        w = world(monkeypatch, tmp_path)
        try:
            under, go = summary_from(w, monkeypatch)
            await go()
            newer_exchanges(w)
            generation = w.store.chat_context('123', 'public:1', '20', True, True)['member_summary']['generation']
            posted = w.message(GENERAL, 'fri what was the plan again')
            w.store.save_chat('123', 'public:1', '20', 0, 'fri what was the plan again', 'you plan raids, '+MARKER, 'm',
                              'high', audience='server', provenance=[], summary_generation=generation,
                              source={'kind': 'discord_message', 'id': str(posted.id), 'user_id': '20',
                                      'display_name': 'friend', 'channel_id': str(GENERAL), 'guild_id': '123'})
            turns = w.fri.agent.saved_turns
            turns.kick = lambda: None  # Its withdrawal stays queued here: Gene hasn't taken it yet.
            if queued:
                turns.withdraw([under], 123, deleted=True)
            carried = []

            async def respond(text, context, ready):
                await ready()
                carried.append(context)
                return ChatResult(status='completed', reply='sure')
            model(w.fri, tmp_path, respond)
            await w.say(GENERAL, 'fri and the time?')
            assert (MARKER in str(carried[-1].get('turns'))) is not queued
            assert (carried[-1].get('member_summary') is None) is queued
        finally:
            await w.fri.close()
    asyncio.run(scenario())


# 3. A DM's and a channel's final fence.

@pytest.mark.parametrize('where', ['a DM', 'a channel'])
@pytest.mark.parametrize('gone', [True, False], ids=['the quoted message deleted', 'nothing'])
def test_a_reply_is_held_to_what_its_saved_context_drew_on(monkeypatch, tmp_path, where, gone):
    """The issue's repro: member 20's saved reply B drew on A, and their next message's reply reads B and repeats it. A
    is deleted during Gene's last check of the turn, its withdrawal still pending: the reply doesn't go out, nor is it
    saved, as a /fri panel reply's isn't."""
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        if where == 'a DM':
            _, message = direct(fri, agent, rows)
        conversation, channel = ('private:555', '555') if where == 'a DM' else ('public:1', '1')
        quoted = source(age=1200, channel='1')
        said = source(age=600, channel=channel)
        store.save_chat('123', conversation, '20', 0, 'what did ash say', REPLY, 'model', 'high', source=said,
                        provenance=[], drew_on=[quoted['id']])
        agent.saved_turns.kept({'guild_id': '123', 'channel_id': channel, 'conversation_id': conversation,
                                'user_id': '20', 'revision': 0, 'source': said, 'drew_on': [quoted['id']]}, {},
                               (time.time(), time.monotonic()))
        answered = []

        async def respond(text, context, ready):
            await ready()
            assert MARKER in str(context.get('turns'))
            answered.append(True)
            return ChatResult(status='completed', reply=REPLY)
        model(fri, tmp_path, respond)
        api, saved, deleting = fri.api, [], []

        async def recording(method, path, payload=None):
            if path.startswith('/chat/turn/withdraw'):
                raise TimeoutError()
            if path == '/chat/turn':
                saved.append(payload)
            answer = await api(method, path, payload)
            if path == '/chat/turn/check' and answered and gone and not deleting:  # Gene's last check.
                deleting.append(path)
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, guild_id=123,
                                                                message_id=int(quoted['id'])))
            return answer
        fri.api = recording
        asked = message('fri say what ash said again')
        asked.reply.return_value = SimpleNamespace(id=asked.id+100, content='okay', edit=None)
        try:
            await agent.handle(asked)
            said_back = str(asked.reply.await_args_list)
            if gone:
                assert deleting and MARKER not in said_back and not saved
            else:
                assert MARKER in said_back and saved
        finally:
            await fri.close()
    asyncio.run(scenario())
