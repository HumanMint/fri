"""A message taken back by an edit stays out of every copy read before it, but a reply to it read after, holding
what it says now, is answered (fri #101). Anything about that timing that can't be shown fails closed."""
import asyncio
import copy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from compat_tests.test_fri_edit_reanswer import edit
from compat_tests.test_fri_edit_reanswer import model as respond_with
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import no_debounce
from compat_tests.test_fri_withdrawn_rows import model as real_model
from fri.fri import WITHDRAWN
from fri.fri_agent import Agent
from fri.fri_chat import ChatResult

MARKER = 'synthetic-private-marker'  # Synthetic words a withdrawal must keep from going anywhere.
CURRENT = 'current safe caption'


async def withdrawn_by_edit(fri, agent, store, message, rows):
    """The issue's setup: a member's message Fri answered, saved with its words; another member's reply to it is
    being answered when the author edits it, so that turn's recheck takes it back (Agent.withdrawn)."""
    earlier = message('fri explain ' + MARKER)
    earlier.reply.return_value = SimpleNamespace(id=earlier.id+100, content='okay', edit=AsyncMock())
    await agent.handle(earlier)
    assert MARKER in str(store.chat_context('123', 'public:1', '20')['turns'])
    intermediate = message('fri explain that caption?', user=30)
    intermediate.reference = SimpleNamespace(message_id=earlier.id, channel_id=1)
    original_handler = fri.chat.transport.handler

    async def editing_during_response(request):
        response = original_handler(request)
        current = copy.copy(earlier)
        current.content = CURRENT
        current.edited_at = discord.utils.utcnow()
        rows[earlier.id] = current
        await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=current))
        return response
    fri.chat.transport.handler = editing_during_response
    await agent.handle(intermediate)
    fri.chat.transport.handler = original_handler
    assert str(earlier.id) in agent.withdrawn
    return earlier


def later_reply(message, earlier):
    later = message('fri explain the current caption?')
    later.reply.return_value = SimpleNamespace(id=later.id+100, content='okay', edit=AsyncMock())
    later.reference = SimpleNamespace(message_id=earlier.id, channel_id=1)
    return later


def test_a_fresh_reply_to_current_edited_text_can_be_answered(monkeypatch, tmp_path):
    """fri #101: for an hour after an edit took a message back, a new reply to it, which fetches what it says now,
    was refused as withdrawn. It's answered from the current text; the old words, which its saved exchange held,
    still reach nothing (fri #129)."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        fri.data = tmp_path
        requests = []
        real_model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)
        try:
            earlier = await withdrawn_by_edit(fri, agent, store, message, rows)
            requests.clear()
            later = later_reply(message, earlier)
            await agent.handle(later)
            assert requests and CURRENT in str(requests[-1]) and MARKER not in str(requests)
            assert later.reply.await_count == 1 and WITHDRAWN not in str(later.reply.await_args)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_target_copy_still_showing_the_old_words_after_its_withdrawal_is_refused(monkeypatch, tmp_path):
    """Guard: Discord's copy can lag an edit event. A reply fetched after the withdrawal but still showing the message
    as it was before that edit holds the old words: refused, as before."""
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        fri.data = tmp_path
        requests = []
        real_model(fri, requests)
        fri.chat_once = type(fri).chat_once.__get__(fri)
        try:
            earlier = await withdrawn_by_edit(fri, agent, store, message, rows)
            rows[earlier.id] = earlier  # Discord still shows it unedited.
            requests.clear()
            later = later_reply(message, earlier)
            await agent.handle(later)
            assert MARKER not in str(requests)
            assert later.reply.await_count == 1 and WITHDRAWN in str(later.reply.await_args)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_to_a_message_answered_again_as_edited_can_be_answered(monkeypatch, tmp_path):
    """fri #101's second repro (the 2026-10-28 combined review): a message edited while Fri answers it is answered
    again as edited; a later reply to it, asking about what it says now, was refused for an hour."""
    async def scenario():
        fri, agent, store, _, _, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri whats teh best ' + MARKER)
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='okay', edit=AsyncMock())
        requests, edited = [], []

        async def respond(text, context, ready):
            await ready()
            requests.append(text+str(context))
            if not edited:
                edited.append(await edit(fri, rows, trigger, 'fri whats the best album'))
                return ChatResult(status='completed', reply='answer to the old words')
            return ChatResult(status='completed', reply='answer to the edit')
        respond_with(fri, tmp_path, respond)
        try:
            await agent.handle(trigger)
            await asyncio.gather(*agent.reruns)
            assert edited[0].reply.await_count == 1 and 'answer to the edit' in str(edited[0].reply.await_args)
            assert str(trigger.id) in agent.withdrawn
            requests.clear()
            later = message('fri and the runner up?', user=21)
            later.reply.return_value = SimpleNamespace(id=later.id+100, content='okay', edit=AsyncMock())
            later.reference = SimpleNamespace(message_id=trigger.id, channel_id=1)
            await agent.handle(later)
            assert requests and 'best album' in requests[-1] and MARKER not in requests[-1]
            assert later.reply.await_count == 1 and WITHDRAWN not in str(later.reply.await_args)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_copy_passes_only_when_read_after_the_withdrawal_and_showing_its_newest_edit():
    """The record itself: a copy of a message taken back passes only if its read began after that withdrawal and it
    shows the newest edit seen of the message. Read before it or in the same step, a withdrawal whose edit Fri never
    saw, a copy showing no edit or an older one, and a newer edit seen since: each fails closed."""
    fri = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, get_channel=lambda _: None)
    agent = Agent(fri)
    agent.saved_turns.withdraw = lambda *args, **kwargs: None
    first = dt.datetime(2026, 11, 3, 12, 0, tzinfo=dt.timezone.utc)
    second = first+dt.timedelta(seconds=5)
    before = agent.withdrawal_mark()
    agent.saw_edit(5, first)
    agent.withdraw(SimpleNamespace(id=5, channel=SimpleNamespace(id=1)))
    after = agent.withdrawal_mark()
    assert agent.read_after_withdrawal(5, after, first.timestamp())
    assert agent.read_after_withdrawal(5, after, second.timestamp())  # A newer edit than any seen: it says that now.
    assert not agent.read_after_withdrawal(5, before, first.timestamp())  # Read before it was taken back.
    assert not agent.read_after_withdrawal(5, None, first.timestamp())  # When it was read isn't known.
    assert not agent.read_after_withdrawal(5, after, None)  # Shows no edit: as it was before.
    assert not agent.read_after_withdrawal(5, after, (first-dt.timedelta(seconds=1)).timestamp())
    agent.edit(1, SimpleNamespace(id=5, author=SimpleNamespace(id=20, bot=False), edited_at=second, content='again'))
    assert not agent.read_after_withdrawal(5, after, first.timestamp())  # A newer edit seen since: it must show it.
    assert agent.read_after_withdrawal(5, after, second.timestamp())
    agent.withdraw(SimpleNamespace(id=6, channel=SimpleNamespace(id=1)))  # Taken back with no edit seen (a delete).
    assert not agent.read_after_withdrawal(6, agent.withdrawal_mark(), second.timestamp())
    agent.withdraw(SimpleNamespace(id=5, channel=SimpleNamespace(id=1)))  # Taken back again: read before that.
    assert not agent.read_after_withdrawal(5, after, second.timestamp())
