"""An edit while Fri answers gets the edited message answered once; Red's commands are Gene's, not Fri's."""
import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import no_debounce
from fri.fri import WITHDRAWN
from fri.fri_chat import ChatResult

MARKER = 'synthetic-private-marker'  # Synthetic words the edit took back.


def model(fri, tmp_path, respond):
    """Fri's own model call, with only the model replaced: `respond(text, context, ready)` runs its last checks."""
    from fri.fri import Fri
    fri.data = tmp_path
    fri.flush_usage = AsyncMock()
    async def signed_in(text, context, ready=None):
        return await respond(text, context, ready)
    async def call(text, context):
        return await Fri.chat_once(fri, text, context)
    fri.chat = SimpleNamespace(respond=signed_in)
    fri.chat_once = AsyncMock(side_effect=call)


async def edit(fri, rows, item, text):
    current = copy.copy(item)
    current.content = text
    current.edited_at = discord.utils.utcnow()
    current.reply = AsyncMock(return_value=SimpleNamespace(id=item.id+100, content='okay', edit=AsyncMock()))
    rows[item.id] = current
    await fri.on_raw_message_edit(SimpleNamespace(channel_id=item.channel.id, message=current))
    return current


@pytest.mark.parametrize('when', ['before the request', 'while the model answers'])
def test_a_message_edited_while_fri_answers_it_is_answered_once_as_edited(monkeypatch, tmp_path, when):
    async def scenario():
        fri, agent, store, _, _, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER)
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='okay', edit=AsyncMock())
        requests, edited = [], []
        async def respond(text, context, ready):
            if not edited and when == 'before the request':
                edited.append(await edit(fri, rows, trigger, 'fri whats the best album'))
            await ready()
            requests.append(text+str(context))
            if not edited:
                edited.append(await edit(fri, rows, trigger, 'fri whats the best album'))
                return ChatResult(status='completed', reply='answer to the old words')
            return ChatResult(status='completed', reply='answer to the edit')
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            await asyncio.gather(*agent.reruns)
            assert not trigger.reply.await_count  # Neither the old answer nor an "ask again" notice.
            assert edited[0].reply.await_count == 1
            assert 'answer to the edit' in str(edited[0].reply.await_args)
            assert 'best album' in requests[-1] and MARKER not in requests[-1]
            turns = store.chat_context('123', 'public:1', '20')['turns']
            assert [turn['input'] for turn in turns] == ['fri whats the best album'] and MARKER not in str(turns)
            assert any(r['id'] == str(trigger.id) and r['text'] == 'fri whats the best album' for r in agent.recent[1])
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_second_edit_gets_the_notice_instead_of_a_third_answer(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER)
        edits = []
        async def respond(text, context, ready):
            await ready()
            if len(edits) < 2:
                edits.append(await edit(fri, rows, trigger, f'fri whats the best album, take {len(edits)+1}'))
            return ChatResult(status='completed', reply=f'answer {len(edits)}')
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            await asyncio.gather(*agent.reruns)
            assert fri.chat_once.await_count == 2
            assert not trigger.reply.await_count
            assert edits[0].reply.await_count == 1 and WITHDRAWN in str(edits[0].reply.await_args)
            assert not edits[1].reply.await_count
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_an_edit_after_a_tool_ran_gets_the_notice_and_is_not_redone(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, _, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri play something good')
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='okay', edit=AsyncMock())
        async def respond(text, context, ready):
            await ready()
            agent.act(trigger.id)  # As Actions.execute marks a tool that may change something.
            await edit(fri, rows, trigger, 'fri play something better')
            return ChatResult(status='completed', reply='queued it')
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            await asyncio.gather(*agent.reruns)
            assert fri.chat_once.await_count == 1
            assert trigger.reply.await_count == 1 and WITHDRAWN in str(trigger.reply.await_args)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_actions_mark_a_tool_that_may_change_something_but_not_a_read():
    from fri.fri_actions import Actions
    marked = []
    actions = SimpleNamespace(event=SimpleNamespace(id=5), agent=SimpleNamespace(act=marked.append),
                              invitation=None, private_evidence=False, conversation={})
    async def scenario(kind):
        try:
            await Actions.execute(actions, {'kind': kind})
        except Exception:
            pass  # Only whether it was marked before anything else ran matters here.
    asyncio.run(scenario('read_recent'))
    assert marked == []
    asyncio.run(scenario('gene_command'))
    assert marked == [5]


@pytest.mark.parametrize('opened', ['follow-up window', 'fri channel'])
def test_gene_commands_are_never_fri_turns(monkeypatch, tmp_path, opened):
    async def scenario():
        fri, agent, store, _, _, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        if opened == 'fri channel':
            agent.dedicated = 1
        else:
            agent.active[(1, 20)] = time.time()+300
        try:
            for text in ('!play join us for a bite', '!stop', '  !skip'):
                command = message(text)
                await agent.handle(command)
                assert not command.reply.await_count
                assert any(r['id'] == str(command.id) for r in agent.recent[1])  # Still in Fri's view.
                assert str(command.id) in agent.direct  # Nor attention's pick.
            assert not fri.chat_once.await_count
            follow_up = message('play join us for a bite')
            await agent.handle(follow_up)
            assert fri.chat_once.await_count == 1
        finally:
            await fri.close()
    asyncio.run(scenario())
