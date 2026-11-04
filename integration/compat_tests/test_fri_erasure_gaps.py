"""More ways words from before a memory erasure reached the model after it (fri #337): a /fri panel turn submitted
before the member turned memory off and on again, and a post from before its author's erasure that a later message
replies to or links. Neither reaches the model now; what's said after the erasure does, as before."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_edit_reanswer import model
from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_panel_and_learning_fences import panel, shown, skipped
from fri.fri_chat import ChatResult

MARKER = 'the cedar marker'


def erase(store, user):
    """Memory off and on again through Gene, so Fri forgot nothing itself."""
    store.personal_control(user, 'disable')
    store.personal_control(user, 'enable')
    time.sleep(0.01)  # Snowflakes count milliseconds: what follows is after it.


@pytest.mark.parametrize('when', ['before the erasure', 'after it'])
def test_a_panel_turn_submitted_before_the_member_erased_is_not_answered(monkeypatch, tmp_path, when):
    """fri #337: member 20 submits /fri text, then turns memory off and on again while the panel's "Thinking…" edit
    waits, before Fri reads their revision. The text reached the model and its answer was shown. Now the panel says it
    skipped that, and nothing is asked or saved. Submitted after the erasure, it's answered as before."""
    async def scenario():
        fri, agent, store, *_ = environment(monkeypatch, tmp_path, channel_id=1)
        asked, saved = [], []

        async def respond(text, context, ready):
            asked.append(text)
            await ready()
            return ChatResult(status='completed', reply='sure thing')

        model(fri, tmp_path, respond)
        api = fri.api

        async def recording(method, path, payload=None):
            if path == '/chat/turn':
                saved.append(payload)
            return await api(method, path, payload)

        fri.api = recording
        if when == 'after it':
            erase(store, '20')
        interaction = panel(fri)
        edit = interaction.edit_original_response.side_effect

        async def thinking(*args, **kwargs):
            if when == 'before the erasure' and not getattr(thinking, 'done', False):
                thinking.done = True
                erase(store, '20')  # While "Thinking…" goes up.
            return SimpleNamespace(id=1234) if edit is None else await edit(*args, **kwargs)

        interaction.edit_original_response.side_effect = thinking
        try:
            await fri.chat_interaction(interaction, 'remember '+MARKER)
            if when == 'before the erasure':
                assert asked == [] and saved == [] and shown(interaction) == skipped()
            else:
                assert asked and saved and 'sure thing' in shown(interaction)
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('where', ['chat', 'ping', 'outside ping', 'link'])
@pytest.mark.parametrize('author', [30, 20])
@pytest.mark.parametrize('when', ['before the erasure', 'after it'])
def test_a_post_from_before_its_authors_erasure_is_no_reply_target(monkeypatch, tmp_path, where, author, when):
    """fri #337: member 30 (or the asker, 20) posts something, turns memory off and on again, and then a new message
    replies to that post or links it. The post reached the model as the reply target, and an answer repeating it could
    be saved: the target's check read only whether memory was on. A post from before its author's erasure is left out,
    as the live view leaves it out; one posted after it is a target as before."""
    async def scenario():
        fri, agent, store, *_, message, calls, rows = environment(monkeypatch, tmp_path,
                                                                  channel_id=8 if where == 'outside ping' else 1)
        if when == 'after it':
            erase(store, str(author))
        post = message('my answer is '+MARKER, user=author, age=0 if when == 'after it' else 60)
        if when == 'before the erasure':
            erase(store, str(author))
        channel = 8 if where == 'outside ping' else 1
        text = {'chat': 'fri what do you make of that?', 'ping': '<@99> what do you make of that?',
                'outside ping': '<@99> what do you make of that?',
                'link': f'fri what about https://discord.com/channels/123/1/{post.id}'}[where]
        event = message(text, user=20)
        if where != 'link':
            event.reference = SimpleNamespace(message_id=post.id, channel_id=channel)
        event.reply.return_value = SimpleNamespace(id=event.id+1, content='hm')
        fri.chat_once.return_value = ChatResult(status='completed', reply='hm')
        try:
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await agent.handle(event)
            fri.chat_once.assert_awaited_once()
            assert (MARKER in str(fri.chat_once.await_args)) is (when == 'after it')
        finally:
            await fri.close()
    asyncio.run(scenario())
