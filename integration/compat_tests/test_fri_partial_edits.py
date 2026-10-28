"""A partial edit, with no author, withdraws what Fri had in hand like a member's edit (fri #135)."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import heard, model, no_debounce, row
from fri.fri_chat import ChatResult

MARKER = 'synthetic-private-marker'  # Synthetic words a withdrawal must keep from going anywhere.


async def partially_edited(fri, rows, channel, item, text='current safe words'):
    """A member's edit as a partial raw event brings it: the new words and an edit time, but no author."""
    data = {'id': str(item.id), 'type': 0, 'content': text, 'edited_timestamp': discord.utils.utcnow().isoformat()}
    partial = discord.Message(state=fri._connection, channel=channel, data=data)
    assert not hasattr(partial, 'author')
    current = copy.copy(item)
    current.content, current.edited_at = text, partial.edited_at
    current.reply = AsyncMock(return_value=SimpleNamespace(id=item.id+100, content='okay', edit=AsyncMock()))
    rows[item.id] = current  # What a fetch reads now.
    await fri.on_raw_message_edit(discord.RawMessageUpdateEvent(data, partial))
    return current


@pytest.mark.parametrize('when', ['before', 'after'])
def test_a_nearby_post_partially_edited_never_reaches_the_model_or_the_reply(monkeypatch, tmp_path, when):
    """'before': edited after the turn copied the live view, before its request (while Fri signs in).
    'after': edited once the request carried it, while the model writes a reply repeating it."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret, trigger = message('someone said ' + MARKER, user=21), message('fri what did they say')
        requested = []
        async def respond(context, ready):
            assert any(MARKER in r['text'] for r in context['recent'])
            if when == 'after':
                await ready()
                assert regret.id in agent.looked and agent.handling[regret.id] == 0
            await partially_edited(fri, rows, channel, regret)
            assert all(r['id'] != str(regret.id) for r in agent.recent[1])
            if when == 'before':
                assert regret.id not in agent.looked
                await ready()
            requested.append(str(context['recent']))
            return ChatResult(status='completed', reply=MARKER if when == 'after' else 'safe reply')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, regret)
            await agent.handle(trigger)
            assert requested
            if when == 'before':
                assert MARKER not in requested[-1]
                assert trigger.reply.await_args.args[0] == 'safe reply'
            else:
                assert MARKER not in str(trigger.reply.await_args_list)
                assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_message_partially_edited_while_fri_answers_it_is_answered_once_as_edited(monkeypatch, tmp_path):
    """Its own message, still being handled: withdrawn as a member's edit is, so the answer to the old words
    stays out and it's answered again as it reads now (fri #126)."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER)
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='okay', edit=AsyncMock())
        edited = []
        async def respond(context, ready):
            await ready()
            if not edited:
                edited.append(await partially_edited(fri, rows, channel, trigger, 'fri whats the best album'))
                assert str(trigger.id) in agent.withdrawn
                return ChatResult(status='completed', reply='answer to the old words')
            return ChatResult(status='completed', reply='answer to the edit')
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            await asyncio.gather(*agent.reruns)
            assert not trigger.reply.await_count
            assert edited[0].reply.await_count == 1 and 'answer to the edit' in str(edited[0].reply.await_args)
            turns = store.chat_context('123', 'public:1', '20')['turns']
            assert [turn['input'] for turn in turns] == ['fri whats the best album'] and MARKER not in str(turns)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_partial_edit_of_a_message_fri_never_saw_marks_nothing(monkeypatch, tmp_path):
    """Raw edits arrive from every channel Fri can see: one for a message it never had in hand marks
    nothing and leaves the live view, and the turn that reads it, as they were."""
    async def scenario():
        fri, agent, _, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        nearby, unseen = message('someone said hi', user=21), message('never heard', user=22)
        trigger = message('fri what did they say')
        async def respond(context, ready):
            await partially_edited(fri, rows, channel, unseen)
            await ready()
            assert any(r['id'] == str(nearby.id) for r in context['recent'])
            return ChatResult(status='completed', reply='safe reply')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, nearby)
            view = [dict(r) for r in agent.recent[1]]
            await partially_edited(fri, rows, channel, unseen)
            assert not agent.withdrawn and not agent.revised
            assert [dict(r) for r in agent.recent[1]] == view
            await agent.handle(trigger)
            assert str(unseen.id) not in agent.withdrawn and unseen.id not in agent.revised
            assert trigger.reply.await_args.args[0] == 'safe reply'
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['fri', 'gene_error'])
def test_a_partial_update_to_fris_or_genes_own_row_leaves_an_ordinary_answer_alone(monkeypatch, tmp_path, kind):
    """Sol r1 of fri #140, S3: whose row it is is known, so it's no member's edit to withhold a reply for."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        bot = message('earlier bot line', user=99 if kind == 'fri' else 77)
        bot.author.bot = True
        cached = row(bot.id, user=str(bot.author.id), **({'fri': True, 'provenance': [{'user_id': '21', 'revision': 0}]}
                                                          if kind == 'fri' else {'gene_error': True}))
        agent.recent[1].append(cached)
        trigger = message('fri hello there')
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='safe answer', edit=AsyncMock())
        async def respond(context, ready):
            await ready()
            await partially_edited(fri, rows, channel, bot, 'bot added a notice')
            assert cached in agent.recent[1]
            return ChatResult(status='completed', reply='safe answer')
        model(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            assert trigger.reply.await_args.args[0].startswith('safe answer')
        finally:
            await fri.close()
    asyncio.run(scenario())
