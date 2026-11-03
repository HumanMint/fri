"""A /fri panel reply waiting for its lock shows nothing drawn from a message taken back meanwhile, even while that
message's withdrawal is still on its way to Gene (fri #306)."""

import asyncio
from types import SimpleNamespace

import pytest

from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_endings import source
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from compat_tests.test_fri_panel_delivery import edits
from fri.fri_chat import ChatResult

MARKER = 'synthetic-withdrawn-marker'
REPLY = 'you said '+MARKER


@pytest.mark.parametrize('moved', ['deleted', 'nothing'])
@pytest.mark.parametrize('first_memory', [False, True], ids=['reply', 'first memory'])
def test_a_panel_reply_waiting_for_its_lock_holds_back_a_message_taken_back_meanwhile(monkeypatch, tmp_path, moved,
                                                                                         first_memory):
    """The issue's repro: member 20's saved exchange holds a marker, and their /fri panel turn reads and repeats it.
    While the reply waits for the panel's lock, that message is deleted, and its withdrawal times out, so Gene still
    holds the exchange: neither revisions nor erasures moved, and the panel showed and saved the marker. The messages
    the model's request carried are checked against those taken back here since the turn began, after Gene's check,
    right before the edit: nothing of it shows or is saved. The first-memory redisplay after the save is held to it
    too. With nothing taken back, it all shows as before."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.capabilities = frozenset((await fri.api('GET', '/fri/contract'))['capabilities'])
        interaction = panel(fri)
        earlier = source(age=60)  # Said in the workshop, where the panel's private conversation is.
        store.save_chat('123', 'private:456', '20', 0, 'remember this: '+MARKER, 'noted', 'model', 'high',
                        source=earlier, provenance=[])

        async def respond(text, context, ready):
            await ready()
            assert MARKER in str(context.get('turns'))  # The request carried the saved exchange.
            return ChatResult(status='completed', reply=REPLY,
                              notes=[{'category': 'games', 'evidence': 'i like chess'}] if first_memory else [])
        model(fri, tmp_path, respond)
        api, saved = fri.api, []
        lock = fri.private_panels.locks.setdefault((123, 20), asyncio.Lock())
        taken, trigger = [], '/chat/turn' if first_memory else '/chat/turn/check'

        async def holder():
            for _ in range(10000):  # Until the delivery waits for the lock (or plainly never does).
                if getattr(lock, '_waiters', None):
                    break
                await asyncio.sleep(0)
            if moved == 'deleted':
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=456, guild_id=123,
                                                                message_id=int(earlier['id'])))
            lock.release()

        async def recording(method, path, payload=None):
            if path.startswith('/chat/turn/withdraw'):
                raise TimeoutError()  # Gene doesn't take the withdrawal: it still holds the exchange.
            if path == '/chat/turn':
                saved.append(payload)
            answer = await api(method, path, payload)
            if path == trigger and not taken:  # Another edit of this panel takes its lock now.
                taken.append(path)
                await lock.acquire()
                asyncio.get_running_loop().create_task(holder())
            return answer
        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'i like chess. what did i tell you to remember')
            assert taken
            if moved == 'nothing':
                assert REPLY in shown(interaction)
                assert edits(interaction, REPLY) == (2 if first_memory else 1)
            elif first_memory:  # The answer showed and was saved before; its redisplay doesn't show.
                assert edits(interaction, REPLY) == 1
            else:
                assert edits(interaction, REPLY) == 0 and not saved and shown(interaction) == skipped()
        finally:
            await fri.close()
    asyncio.run(scenario())
