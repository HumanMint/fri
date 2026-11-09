"""A memory control must be said to Fri: in the dedicated channel, where Fri also answers plain chat, a member's
"forget me" said to someone else never erases their memory (review of fri #195)."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
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


@pytest.mark.parametrize('text', ['forget me', 'forget about me'])
def test_a_control_withdrawn_during_its_last_reply_target_lookup_is_not_carried_out(monkeypatch, tmp_path, text):
    """The message is read again after every reply-target lookup, so an edit that lands during the last one, before
    its gateway event arrives, still takes the control back (review of fri #195, round 5)."""
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            agent.dedicated = 0
            await remembered(agent, store, message)
            agent.active.clear()
            frisays = message('noted', user=99)
            control = message(text)
            control.reference = SimpleNamespace(message_id=frisays.id, channel_id=1, resolved=None)
            original, lookups = channel.fetch_message.side_effect, []
            def fetch(mid):
                if mid == frisays.id:
                    lookups.append(mid)
                    if len(lookups) == 2:  # The last lookup, inside the turn: edited meanwhile, its event delayed.
                        edited = copy.copy(control)
                        edited.content = 'never mind'
                        rows[control.id] = edited
                return original(mid)
            channel.fetch_message.side_effect = fetch
            await agent.handle(control)
            assert len(lookups) >= 2
            assert memory(store) == (True, True)
            assert not any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            await fri.close()
    asyncio.run(scenario())


async def on_a_call(fri, channel, message):
    """/jpcall running in this channel, as main Fri sees it: the call manager's posts there, Fri's messages (see
    DiscordCalls.posted and posting_in). The real manager records them in tests/jpcall; Gene's venv lacks its voice deps."""
    post = message('a question for the requester · will you use the parking lot?', user=99)
    fri.jpcall_calls = SimpleNamespace(posted=lambda: frozenset({post.id}), posting_in=lambda cid: cid == channel.id)
    return fri.jpcall_calls, post


def to(post):
    return SimpleNamespace(message_id=post.id, channel_id=1, resolved=post)


@pytest.mark.parametrize('text', ['forget me', 'forget about me'])
def test_a_memory_control_replying_to_a_running_calls_post_is_still_one(monkeypatch, tmp_path, text):
    """Ordinary replies to a call's posts are the call's, never chat Fri's; a privacy control said to that post of
    Fri's keeps its usual path, rechecks and all, and so does "remember me again" (CORE-1 review)."""
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            agent.dedicated = 0
            await remembered(agent, store, message)
            agent.active.clear()
            _, post = await on_a_call(fri, channel, message)
            control = message(text)
            control.reference = to(post)
            await agent.handle(control)
            assert memory(store) == (False, False)
            assert 'erased' in control.reply.await_args.args[0]
            again = message('remember me again')
            again.reference = to(post)
            await agent.handle(again)
            assert memory(store)[0] is True
        finally:
            fri.jpcall_calls = None  # The call ended.
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['forget me', 'forget about me'])
def test_a_plain_memory_control_in_fris_chat_channel_still_works_during_a_call(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            agent.active.clear()  # Fri's channel itself lets it in, not a follow-up window.
            await on_a_call(fri, channel, message)
            control = message(text)
            await agent.handle(control)
            assert memory(store) == (False, False)
            assert 'erased' in control.reply.await_args.args[0]
        finally:
            fri.jpcall_calls = None  # The call ended.
            await fri.close()
    asyncio.run(scenario())


def test_ordinary_lines_during_a_call_stay_with_the_call_but_explicit_chat_doesnt(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            agent.active.clear()  # No follow-up window: a line right after chatting with Fri still is one, call or not.
            fri.chat_once.reset_mock()
            _, post = await on_a_call(fri, channel, message)
            plain, answer = message('ask if they have parking'), message('yes, one car', user=30)
            answer.reference = to(post)
            for event in (plain, answer):
                await agent.handle(event)
                event.reply.assert_not_awaited()
            fri.chat_once.assert_not_awaited()
            assert memory(store) == (True, True)
            await agent.handle(named := message('fri what do you think'))
            fri.chat_once.assert_awaited()
            named.reply.assert_awaited()
        finally:
            fri.jpcall_calls = None  # The call ended.
            await fri.close()
    asyncio.run(scenario())


def test_a_forget_me_said_to_someone_else_in_fris_chat_channel_during_a_call_still_isnt_one(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            await remembered(agent, store, message)
            await on_a_call(fri, channel, message)
            theirs = message('ugh this game', user=30)
            control = message('forget me')
            control.reference = SimpleNamespace(message_id=theirs.id, channel_id=1, resolved=theirs)
            await agent.handle(control)
            assert memory(store) == (True, True)
            assert not any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            fri.jpcall_calls = None  # The call ended.
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['edited', 'deleted'])
def test_a_forget_me_to_a_call_post_taken_back_during_its_reply_target_lookup_is_not_carried_out(
        monkeypatch, tmp_path, change):
    """As for a reply to any of Fri's messages: the control is read again after the turn's last lookup of the post it
    replies to, so one edited or deleted during it, before its gateway event, is not carried out."""
    async def scenario():
        fri, agent, store, channel, message, calls, rows = seeded(monkeypatch, tmp_path)
        try:
            agent.dedicated = 0
            await remembered(agent, store, message)
            agent.active.clear()
            _, post = await on_a_call(fri, channel, message)
            control = message('forget me')
            control.reference = SimpleNamespace(message_id=post.id, channel_id=1, resolved=None)
            original, lookups = channel.fetch_message.side_effect, []
            def fetch(mid):
                if mid == post.id:
                    lookups.append(mid)
                    if len(lookups) == 2:  # The last lookup, inside the turn.
                        if change == 'edited':
                            edited = copy.copy(control)
                            edited.content = 'never mind'
                            rows[control.id] = edited
                        else:
                            del rows[control.id]
                if mid not in rows:
                    raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
                return original(mid)
            channel.fetch_message.side_effect = fetch
            await agent.handle(control)
            assert len(lookups) >= 2
            assert memory(store) == (True, True)
            assert not any(path == '/chat/memory/control' for path, _ in calls)
        finally:
            fri.jpcall_calls = None  # The call ended.
            await fri.close()
    asyncio.run(scenario())
