"""A /fri panel shows a turn's words only after its lock is taken and its checks pass again, the first-memory edit
included (fri #293)."""

import asyncio

import pytest

from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from fri.fri_chat import ChatResult

REPLY = 'ok noted, chess it is'


def edits(interaction, words):
    """How many of the panel's edits carried these words."""
    return sum(words in ' '.join(embed.description or '' for embed in call.kwargs.get('embeds') or [])
               for call in interaction.edit_original_response.await_args_list)


@pytest.mark.parametrize('moved', ['local', 'gene', 'nothing'])
def test_a_panel_reply_waiting_for_its_lock_is_held_to_its_authors_erasures(monkeypatch, tmp_path, moved):
    """fri #293: the panel's checks pass, then its delivery waits for the panel's lock (another edit of the same panel
    holding it), and the author erases meanwhile, here or through Gene: the answer was still shown once the lock came
    free. The checks run again after the lock is taken, right before the edit: nothing of it is shown or saved. With
    nothing changed it's shown as before."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        lock = fri.private_panels.locks.setdefault((123, 20), asyncio.Lock())

        async def holder():
            while not getattr(lock, '_waiters', None):  # Until the reply waits for the lock.
                await asyncio.sleep(0)
            if moved == 'local':
                agent.forget(20, erased=True)
            elif moved == 'gene':
                store.personal_control('20', 'disable')
            lock.release()

        async def respond(text, context, ready):
            await ready()
            await lock.acquire()  # Another edit of this panel, taking the lock once the "Thinking…" one let it go.
            asyncio.get_running_loop().create_task(holder())
            return ChatResult(status='completed', reply=REPLY)

        model(fri, tmp_path, respond)
        api, saved = fri.api, []

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            return await api(method, path, payload)

        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'how was your day')
            if moved == 'nothing':
                assert REPLY in shown(interaction) and saved
                return
            assert edits(interaction, REPLY) == 0 and not saved
            if moved == 'local':
                assert shown(interaction) == skipped()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_panel_reply_is_held_to_an_erasure_here_during_its_check_in_the_lock(monkeypatch, tmp_path):
    """#297's round 1: nothing else holds the panel's lock, Gene passes the turn's check inside it, and the author's
    "forget me" lands here before that answer comes back. The panel showed the reply anyway. Their erasures here are read
    again after that check, with nothing awaited before the edit: nothing of it is shown or saved."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)

        async def respond(text, context, ready):
            await ready()
            return ChatResult(status='completed', reply=REPLY)

        model(fri, tmp_path, respond)
        api, saved, inside = fri.api, [], []

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            answer = await api(method, path, payload)
            lock = fri.private_panels.locks.get((123, 20))
            if path == '/chat/turn/check' and lock is not None and lock.locked():
                inside.append(path)
                agent.forget(20, erased=True)  # Gene said yes, and the "forget me" here lands before its answer does.
            return answer

        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'how was your day')
            assert inside and edits(interaction, REPLY) == 0 and not saved
            assert shown(interaction) == skipped()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['local', 'gene', 'someone else', 'nothing'])
def test_a_first_memory_edit_is_held_to_its_authors_erasures(monkeypatch, tmp_path, moved):
    """fri #293: a genuine first memory shows the answer again with its notice once the turn is saved. Someone erasing
    while it was being saved still saw that edit show the old answer again: the author here, the author through Gene,
    or member 30 through Gene, whose words the reply drew on. Saving the turn cleared what Gene's check reads, so that
    edit had nothing to check (#297's round 1). The whole turn is checked again after the lock, then the author's
    erasures here: with any of them moved, the answer isn't shown again. With nothing changed it is, notice and all."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        store.save_chat('123', 'private:456', '20', 0, 'what did they say', 'member thirty plays the oud', 'model',
                        'high', provenance=[{'user_id': '30', 'revision': 0}])

        async def respond(text, context, ready):
            await ready()
            return ChatResult(status='completed', reply=REPLY,
                              notes=[{'category': 'games', 'evidence': 'i like chess'}])

        model(fri, tmp_path, respond)
        api, first, drew = fri.api, [], []

        async def recording(method, path, payload=None):
            answer = await api(method, path, payload)
            if path == '/chat/turn':
                first.append(bool(answer.get('first_memory')))
                drew.append(any(row.get('user_id') == '30' for row in payload.get('provenance') or []))
                if moved == 'local':
                    agent.forget(20, erased=True)  # Taken in while the turn was being saved.
                elif moved == 'gene':
                    store.personal_control('20', 'disable')
                elif moved == 'someone else':
                    store.personal_control('30', 'disable')
            return answer

        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'i like chess')
            assert first == [True] and drew == [True]  # A genuine first memory, drawing on member 30's words.
            assert edits(interaction, REPLY) == (2 if moved == 'nothing' else 1)
        finally:
            await fri.close()
    asyncio.run(scenario())
