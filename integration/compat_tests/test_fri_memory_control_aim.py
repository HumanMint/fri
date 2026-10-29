"""A memory control must be said to Fri: in the dedicated channel, where Fri also answers plain chat, a member's
"forget me" said to someone else never erases their memory (review of fri #195)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import no_debounce

MARKER = 'synthetic-saved-preference'


def seeded(monkeypatch, tmp_path):
    """The dedicated channel, with something of member 20's saved and memory on."""
    fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
    no_debounce(monkeypatch)
    agent.dedicated = 1
    return fri, agent, store, channel, message, calls, rows


async def remembered(agent, store, message):
    earlier = message('fri remember ' + MARKER)
    earlier.reply.return_value = SimpleNamespace(id=earlier.id+100, content='okay', edit=AsyncMock())
    await agent.handle(earlier)
    assert store.chat_context('123', 'public:1', '20')['turns']


def memory(store):
    context = store.chat_context('123', 'public:1', '20')
    return context['personal_memory']['enabled'], bool(context['turns'])


@pytest.mark.parametrize('text', ['forget about me', 'forget me pls', 'forget me'])
def test_a_forget_me_replying_to_another_member_is_not_a_memory_control(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            theirs = message('ugh this game', user=30)
            control = message(text)
            control.reference = SimpleNamespace(message_id=theirs.id, channel_id=1, resolved=theirs)
            await agent.handle(control)
            assert memory(store) == (True, True)
            assert not any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_forget_me_that_mentions_another_member_is_not_a_memory_control(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            control = message('forget about me <@30>')
            control.content = '<@30> forget about me'
            control.mentions = [SimpleNamespace(id=30, bot=False)]
            await agent.handle(control)
            assert memory(store) == (True, True)
            assert not any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text, to_fri', [('forget about me', True), ('fri forget about me', False)])
def test_a_forget_me_said_to_fri_still_erases(monkeypatch, tmp_path, text, to_fri):
    """Replying to Fri, or naming Fri while replying to someone else, is still a request to Fri."""
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            target = message('fri said this', user=99) if to_fri else message('ugh this game', user=30)
            control = message(text)
            control.reference = SimpleNamespace(message_id=target.id, channel_id=1, resolved=target)
            await agent.handle(control)
            assert memory(store) == (False, False)
            assert any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['forget about me', 'forget me pls', 'forget me'])
def test_attention_never_carries_out_a_memory_control(monkeypatch, tmp_path, text):
    """Ambient attention can pick a message that was edited into a memory control while it chose (review of fri #195,
    round 2): it isn't said to Fri, so it changes nothing."""
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            theirs = message('can anyone help?', user=30)
            picked = message(text)
            picked.reference = SimpleNamespace(message_id=theirs.id, channel_id=1, resolved=theirs)
            agent.ambient_ready = lambda channel, user: True  # Attention picks only a message it's ready to answer.
            await agent.respond(picked, text, ambient=True)
            assert memory(store) == (True, True)
            assert not any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['forget about me', 'forget me pls'])
def test_a_forget_me_said_to_someone_else_leaves_local_context_alone(monkeypatch, tmp_path, text):
    """Dropped before Fri clears anything locally, so a reply of Fri's still on its way to that member isn't
    cancelled by it (review of fri #195, round 2)."""
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            before = agent.forgotten.get(20, 0)
            theirs = message('ugh this game', user=30)
            control = message(text)
            control.reference = SimpleNamespace(message_id=theirs.id, channel_id=1, resolved=theirs)
            await agent.handle(control)
            assert agent.forgotten.get(20, 0) == before
            assert memory(store) == (True, True)
        finally:
            await fri.close()
    asyncio.run(scenario())
