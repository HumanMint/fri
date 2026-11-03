"""A /fri panel shows nothing a member erased while it waited: the words of anyone a reply drew on (fri #301), the My
memory listing (fri #302), and each later page of a settings preview (fri #303)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from compat_tests.test_fri_panel_delivery import edits
from fri.fri_chat import ChatResult

REPLY = 'member thirty plays the oud, apparently'
THEIRS = [{'user_id': '30', 'revision': 0}]


def drew_on_thirty(store, user='20'):
    """The member's saved exchange with Fri, built from member 30's words: a reply here inherits them."""
    store.save_chat('123', 'private:456', user, 0, 'what did they say', 'member thirty plays the oud', 'model', 'high',
                    provenance=THEIRS)


def held(fri, user, during):
    """The panel's lock, taken by another edit of the same panel once `during` returns True for an API call, and
    released after `moved()` runs, once something waits for it. Returns the API wrapper's install function."""
    lock = fri.private_panels.locks.setdefault((123, user), asyncio.Lock())
    api, taken = fri.api, []

    def install(moved):
        async def holder():
            while not getattr(lock, '_waiters', None):  # Until the delivery waits for the lock.
                await asyncio.sleep(0)
            moved()
            lock.release()

        async def recording(method, path, payload=None):
            answer = await api(method, path, payload)
            if not taken and during(path, payload):
                taken.append(path)
                await lock.acquire()
                asyncio.get_running_loop().create_task(holder())
            return answer
        fri.api = recording
        return taken
    return install


@pytest.mark.parametrize('moved', ['source', 'someone else', 'nothing'])
def test_a_panel_reply_waiting_for_its_lock_is_held_to_its_sources_erasures_here(monkeypatch, tmp_path, moved):
    """fri #301: member 20's reply carries member 30's words, from their saved exchange. While its delivery waits for
    the panel's lock, member 30's "forget me" lands here, before Gene has it: Gene's check of the turn passes, and the
    panel showed the reply. Every source's erasures here are held to their baseline as the turn came in: nothing is
    shown or saved. A member it didn't draw on changes nothing."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        drew_on_thirty(store)

        async def respond(text, context, ready):
            await ready()
            return ChatResult(status='completed', reply=REPLY)

        model(fri, tmp_path, respond)
        saved, api = [], fri.api

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            return await api(method, path, payload)
        fri.api = recording
        install = held(fri, 20, lambda path, payload: path == '/chat/turn/check' and any(
            row['user_id'] == '30' for row in payload.get('provenance') or []))
        taken = install(lambda: agent.forget({'source': 30, 'someone else': 40}[moved], erased=True)
                        if moved != 'nothing' else None)
        try:
            await fri.chat_interaction(interaction, 'what did member thirty say')
            assert taken  # The turn drew on member 30, and Gene's check of it passed.
            if moved == 'source':
                assert edits(interaction, REPLY) == 0 and not saved
                assert shown(interaction) == skipped()
            else:
                assert REPLY in shown(interaction) and saved
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_panel_reply_is_held_to_a_sources_erasure_here_during_its_check_in_the_lock(monkeypatch, tmp_path):
    """fri #301, inside the lock: Gene passes the turn's check there, and member 30's "forget me" lands here before
    that answer comes back. Their erasures here are read again after the check, with nothing awaited before the
    edit: nothing of the reply is shown or saved."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        drew_on_thirty(store)

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
                agent.forget(30, erased=True)
            return answer

        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'what did member thirty say')
            assert inside and edits(interaction, REPLY) == 0 and not saved
            assert shown(interaction) == skipped()
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('moved', ['local', 'gene', 'both', 'nothing'])
def test_my_memory_waiting_for_its_lock_is_held_to_their_erasures(monkeypatch, tmp_path, moved):
    """fri #302: My memory reads the member's preferences, then waits for the panel's lock while they erase, here
    ("forget me" taken in), through Gene, or both. The panel showed the erased preference and "Memory is on". The
    listing is held to their erasures here and their revision once the lock is taken: nothing of it is shown, only
    the panel's line for something that changed. With nothing changed it's listed as before."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        interaction.channel = SimpleNamespace(id=456, parent_id=None, guild=SimpleNamespace(id=123),
                                              permissions_for=lambda member: discord.Permissions.all())  # The workshop.
        store.save_chat('123', 'private:456', '20', 0, 'i like chess', 'noted', 'model', 'high',
                        notes=[{'category': 'games', 'evidence': 'i like chess'}])

        def erase():
            if moved in ('local', 'both'):
                agent.forget(20, erased=True)
            if moved in ('gene', 'both'):
                store.personal_control('20', 'disable')
        taken = held(fri, 20, lambda path, payload: path == '/chat/memory/notes')(erase)
        try:
            await fri.chat_interaction(interaction, 'show my memory')
            assert taken
            if moved == 'nothing':
                assert 'i like chess' in shown(interaction) and 'Memory is on.' in shown(interaction)
            else:
                assert edits(interaction, 'i like chess') == 0 and edits(interaction, 'Memory is on.') == 0
                assert shown(interaction) == skipped()
        finally:
            await fri.close()
    asyncio.run(scenario())


MARKER = 'thirty-oud-marker'
VOICE = ' '.join(['keep it dry and short.']*40)+' and say '+MARKER+' sometimes.'


def click():
    """A Next or Back click on the preview, its own checks passed (they're the preview's, not the fence's)."""
    return SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))


@pytest.mark.parametrize('moved', ['gene', 'local', 'nothing'])
def test_a_settings_previews_later_page_is_held_to_its_sources_erasures(monkeypatch, tmp_path, moved):
    """fri #303: the owner's two-page voice preview draws on member 30's words, their marker on page two. Member 30
    erases through Gene, or here, and the owner clicks Next: the marker showed. Each later page is held to the turn's
    whole fence inside the preview's lock: nothing more of it shows, and the preview ends. With nothing changed,
    page two shows as before."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        interaction = panel(fri)
        interaction.user = SimpleNamespace(id=10, display_name='owner')  # Only the owner gets a settings preview.
        drew_on_thirty(store, user='10')

        async def respond(text, context, ready):
            await ready()
            return ChatResult(status='completed', action={'kind': 'preview_voice', 'voice_notes': VOICE})

        model(fri, tmp_path, respond)
        api, checked = fri.api, []

        async def recording(method, path, payload=None):
            if path == '/chat/turn/check':
                checked.append(any(row['user_id'] == '30' for row in payload.get('provenance') or []))
            return await api(method, path, payload)
        fri.api = recording
        try:
            await fri.chat_interaction(interaction, 'sharper voice please')
            view = interaction.edit_original_response.await_args.kwargs['view']
            assert len(view.pages) == 2 and MARKER not in view.content and MARKER in view.pages[1]
            assert any(checked)  # The preview's turn drew on member 30.
            view.interaction_check = AsyncMock(return_value=True)
            if moved == 'gene':
                store.personal_control('30', 'disable')
            elif moved == 'local':
                agent.forget(30, erased=True)
            clicked = click()
            await view.next(clicked)
            content = clicked.response.edit_message.await_args.kwargs['content']
            if moved == 'nothing':
                assert MARKER in content and not view.finished
            else:
                assert MARKER not in content and view.finished
                assert all(MARKER not in str(call.kwargs.get('content'))
                           for call in clicked.response.edit_message.await_args_list)
        finally:
            await fri.close()
    asyncio.run(scenario())
