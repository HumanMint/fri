"""A nearby row a turn copied, pushed out of the live view by newer lines while the turn waited, never
brings its old words to the model, a reply or the saved chat once it's edited (fri #141)."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import heard, model, no_debounce
from fri.fri_chat import ChatResult

MARKER = 'synthetic-private-marker'  # Synthetic words an edit must keep from going anywhere.


async def edit(fri, rows, channel, item, kind, text='current safe words'):
    """A member's edit as Discord brings it: the whole message ('full'), or a partial raw event with the new
    words and an edit time but no author ('partial')."""
    current = copy.copy(item)
    current.content, current.edited_at = text, discord.utils.utcnow()
    rows[item.id] = current  # What a fetch reads now.
    if kind == 'full':
        return await fri.on_raw_message_edit(SimpleNamespace(channel_id=channel.id, message=current))
    data = {'id': str(item.id), 'type': 0, 'content': text, 'edited_timestamp': current.edited_at.isoformat()}
    partial = discord.Message(state=fri._connection, channel=channel, data=data)
    assert not hasattr(partial, 'author')
    await fri.on_raw_message_edit(discord.RawMessageUpdateEvent(data, partial))


async def flood(agent, message, item, looked=False):
    """Thirty newer lines push `item` out of the live view. Fri has no other hold on it, unless the request's
    last checks already registered it (`looked`)."""
    for n in range(30):
        await heard(agent, message(f'ordinary later line {n}', user=22))
    assert all(r['id'] != str(item.id) for r in agent.recent[1])
    assert (item.id in agent.looked) == looked and not agent.handling[item.id]


@pytest.mark.parametrize('kind', ['full', 'partial'])
@pytest.mark.parametrize('when', ['before', 'during', 'after'])
def test_a_copied_row_pushed_out_then_edited_never_leaks_its_old_words(monkeypatch, tmp_path, kind, when):
    """Pushed out while Fri signs in ('during': while the request's last checks wait on Gene), then edited:
    'before' the last checks, 'during' them, or 'after' the request, while the model writes a reply repeating it."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('someone said ' + MARKER, user=21)
        trigger = message('fri what did they say')
        trigger.reply.side_effect = lambda text, **_: SimpleNamespace(id=trigger.id+100, content=text, edit=AsyncMock())
        requested = []
        async def respond(context, ready):
            assert any(MARKER in r['text'] for r in context['recent'])  # Copied before the waits.
            if when != 'during':
                await flood(agent, message, regret)
            if when == 'before':
                await edit(fri, rows, channel, regret, kind)
            await ready()
            requested.append(str(context['recent']))
            if when == 'after':
                await edit(fri, rows, channel, regret, kind)
            return ChatResult(status='completed', reply=MARKER if MARKER in requested[-1] else 'safe answer')
        model(fri, tmp_path, respond)
        api, checks = fri.api, []
        async def edited_during_the_last_checks(method, path, payload=None):
            if when == 'during' and path == '/chat/turn/check' and not checks:
                checks.append(path)
                await flood(agent, message, regret, looked=True)
                await edit(fri, rows, channel, regret, kind)
            return await api(method, path, payload)
        fri.api = edited_during_the_last_checks
        try:
            await heard(agent, regret)
            await agent.handle(trigger)
            assert requested and (checks or when != 'during')
            if when != 'after':  # Edited before the request: the old words never reach the model.
                assert MARKER not in str(requested)
            assert MARKER not in str(trigger.reply.await_args_list)
            assert MARKER not in str(store.chat_context('123', 'public:1', '20')['turns'])
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_copied_row_pushed_out_then_deleted_never_reaches_the_model(monkeypatch, tmp_path):
    """Deleted instead, by its author or a moderator: nothing marks it either once it left the live view."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('someone said ' + MARKER, user=21)
        trigger = message('fri what did they say')
        trigger.reply.side_effect = lambda text, **_: SimpleNamespace(id=trigger.id+100, content=text, edit=AsyncMock())
        requested = []
        async def respond(context, ready):
            await flood(agent, message, regret)
            rows.pop(regret.id)
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=regret.id))
            await ready()
            requested.append(str(context['recent']))
            return ChatResult(status='completed', reply=MARKER if MARKER in requested[-1] else 'safe answer')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, regret)
            await agent.handle(trigger)
            assert requested and MARKER not in str(requested)
            assert MARKER not in str(trigger.reply.await_args_list)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['none', 'full edit'])
def test_copied_rows_still_in_the_live_view_reach_the_model_and_the_reply_goes_out(monkeypatch, tmp_path, change):
    """An ordinary turn: the nearby rows it copied are still the live view's, untouched or edited in place (the
    row shows what they left), so the model reads them as they are now and the answer is sent and saved."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        nearby = [message(f'nearby line {n}', user=21+n) for n in range(3)]
        trigger = message('fri what did they say')
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='they said stuff', edit=AsyncMock())
        requested = []
        async def respond(context, ready):
            if change == 'full edit':
                await edit(fri, rows, channel, nearby[1], 'full', 'nearby line fixed')
            await ready()
            requested.append([r['text'] for r in context['recent']])
            return ChatResult(status='completed', reply='they said stuff')
        model(fri, tmp_path, respond)
        try:
            for item in nearby:
                await heard(agent, item)
            await agent.handle(trigger)
            texts = ['nearby line 0', 'nearby line fixed' if change == 'full edit' else 'nearby line 1', 'nearby line 2']
            assert all(text in requested[0] for text in texts)
            assert trigger.reply.await_args.args[0] == 'they said stuff'
            assert 'they said stuff' in str(store.chat_context('123', 'public:1', '20')['turns'])
        finally:
            await fri.close()
    asyncio.run(scenario())
