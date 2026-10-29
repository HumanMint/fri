"""A memory control must be said to Fri: in the dedicated channel, where Fri also answers plain chat, a member's
"forget me" said to someone else never erases their memory (review of fri #195)."""
import asyncio
import copy
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


@pytest.mark.parametrize('text', ['fri forget about me', 'fri forget me pls'])
def test_the_no_agent_fallback_rechecks_a_memory_control_after_typing_starts(monkeypatch, tmp_path, text):
    """Without the agent, a control edited while Fri started typing is read as it is now, not as it came in
    (review of fri #195, round 3)."""
    from contextlib import asynccontextmanager
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            fri.agent = None
            fri.workshop_id = 1
            control = message(text)

            @asynccontextmanager
            async def typing():  # Edited into ordinary chat while typing starts.
                edited = copy.copy(control)
                edited.content = 'fri what about the game tonight'
                rows[control.id] = edited
                yield
            channel.typing = typing
            await fri.on_message(control)
            assert memory(store) == (True, True)
            assert not any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['forget about me', 'forget me pls'])
def test_an_unaddressed_forget_me_outside_a_conversation_leaves_local_context_alone(monkeypatch, tmp_path, text):
    """In an ordinary watched channel, with no conversation going, a bare "forget me" isn't said to Fri: it's dropped
    without clearing anything (review of fri #195, round 3)."""
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            agent.dedicated = 0
            await remembered(agent, store, message)
            agent.active.clear()
            before = agent.forgotten.get(20, 0)
            await agent.handle(message(text))
            assert agent.forgotten.get(20, 0) == before
            assert memory(store) == (True, True)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_to_fri_found_on_a_second_look_is_still_a_memory_control(monkeypatch, tmp_path):
    """The first read of its reply target failed for a moment; the second shows it's a reply to Fri, so the control
    goes ahead (review of fri #195, round 3)."""
    import discord
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            agent.dedicated = 0
            await remembered(agent, store, message)
            agent.active.clear()
            frisays = message('noted', user=99)
            control = message('forget about me')
            control.reference = SimpleNamespace(message_id=frisays.id, channel_id=1, resolved=None)
            original, failed = channel.fetch_message.side_effect, []
            def fetch(mid):
                if mid == frisays.id and not failed:
                    failed.append(mid)
                    raise discord.HTTPException(SimpleNamespace(status=503, reason='unavailable'), 'try again')
                return original(mid)
            channel.fetch_message.side_effect = fetch
            await agent.handle(control)
            assert failed
            assert memory(store) == (False, False)
        finally:
            await fri.close()
    asyncio.run(scenario())
