"""Replies to Fri's own posts: its saved replies and the patch notes it posted, under the same fences."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_endings import source
from compat_tests.test_fri_mentions import environment
from fri.fri import UNATTRIBUTED, UNAVAILABLE
from fri.fri_chat import ChatResult

NOTES = 'fri 1.0.0. added: meme memory, so i stop asking what the cat is. fixed: reminders that never showed up.'


def posted(tmp_path, *parts, name='patch-notes-server.json'):
    """A patch-notes record as fri_patch_notes.send leaves it: each part's Discord message and channel."""
    tried = {'attempted_at': 1790000000, 'sha256': 'a'*64}
    entry = {'sha256': 'b'*64, 'parts': [{**tried, 'message_id': str(mid), 'channel_id': str(channel)}
                                         for mid, channel in parts]}
    (tmp_path/name).write_text(json.dumps({'versions': {'1.0.0': entry}}))


def fri_post(message, text, age):
    post = message(text, user=99, age=age)
    post.author.bot = True
    return post


async def answered(agent, fri, event):
    with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
        await agent.handle(event)
    return fri.chat_once.await_args.args[1]


def test_reply_to_patch_notes_gets_the_notes_text(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        notes = fri_post(message, NOTES, 3600)
        posted(tmp_path, (notes.id, channel.id))
        event = message('fri wait whats meme memory', user=40)
        event.reference = SimpleNamespace(message_id=notes.id, channel_id=channel.id)
        fri.chat_once.return_value = ChatResult(status='completed', reply='i remember memes now. try to keep up')
        try:
            context = await answered(agent, fri, event)
            assert context['reply_target'][0]['text'] == NOTES and 'unattributed_reference' not in context
            assert context['reference_provenance'] == []  # Fri's own words, drawing on no member.
            saved = store.chat_context('123', 'public:1', '40')['turns']
            assert saved[-1]['reply'] == 'i remember memes now. try to keep up'
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_reply_to_fris_answer_about_patch_notes_gets_fris_words(monkeypatch, tmp_path):
    """A thread under the notes: Fri's answer to them is saved as said, so a reply to that answer resolves too."""
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        notes = fri_post(message, NOTES, 3600)
        posted(tmp_path, (notes.id, channel.id))
        first = message('fri wait whats meme memory', user=40, age=60)
        first.reference = SimpleNamespace(message_id=notes.id, channel_id=channel.id)
        reply = 'i remember memes now. try to keep up'
        first.reply.return_value = answer = fri_post(message, reply, 50)
        fri.chat_once.return_value = ChatResult(status='completed', reply=reply)
        try:
            await answered(agent, fri, first)
            event = message('fri since when', user=20)
            event.reference = SimpleNamespace(message_id=answer.id, channel_id=channel.id)
            fri.chat_once.return_value = ChatResult(status='completed', reply='since 1.0.0, read the pin')
            context = await answered(agent, fri, event)
            assert context['reply_target'][0]['text'] == reply and 'unattributed_reference' not in context
            assert context['reference_provenance'] == [{'user_id': '40', 'revision': 0}]
            assert store.chat_context('123', 'public:1', '20')['turns'][-1]['reply'] == 'since 1.0.0, read the pin'
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_reply_to_a_saved_fri_answer_gets_fris_words(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        posted(tmp_path, (1234, channel.id))  # Notes on record, so their lookup runs beside Gene's.
        old = fri_post(message, 'ash is moving to denver next month', 20)
        store.save_chat('123', 'public:1', '20', 0, 'where is ash moving', old.content, 'm', 'high',
                        source=source(age=30, channel='1'), provenance=[{'user_id': '30', 'revision': 0}],
                        reply_message_id=str(old.id), verbatim=True)
        event = message('fri wait where?', user=40)
        event.reference = SimpleNamespace(message_id=old.id, channel_id=channel.id)
        fri.chat_once.return_value = ChatResult(status='completed', reply='denver. keep up')
        try:
            context = await answered(agent, fri, event)
            assert context['reply_target'][0]['text'] == old.content and 'unattributed_reference' not in context
            assert context['reference_provenance'] == [{'user_id': '30', 'revision': 0},
                                                       {'user_id': '20', 'revision': 0}]
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['erase', 'disable'])
def test_fri_answer_whose_source_erased_stays_unavailable(monkeypatch, tmp_path, change):
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        old = fri_post(message, 'ash is moving to denver next month', 20)
        posted(tmp_path, (1234, channel.id))  # Notes on record: they never stand in for a reply Gene dropped.
        store.save_chat('123', 'public:1', '20', 0, 'where is ash moving', old.content, 'm', 'high',
                        source=source(age=30, channel='1'), provenance=[{'user_id': '30', 'revision': 0}],
                        reply_message_id=str(old.id), verbatim=True)
        store.erase_user('30') if change == 'erase' else store.personal_control('30', 'disable')
        event = message('fri wait where?', user=40)
        event.reference = SimpleNamespace(message_id=old.id, channel_id=channel.id)
        fri.chat_once.return_value = ChatResult(status='completed', reply='which one? i dont have that anymore')
        try:
            context = await answered(agent, fri, event)
            assert context['reply_target'][0]['text'] == UNAVAILABLE and context['unattributed_reference']
            assert 'denver' not in str(context['reply_target'])
            assert store.chat_context('123', 'public:1', '40')['turns'][-1]['reply'] == UNATTRIBUTED
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('record', ['none', 'elsewhere', 'other-channel', 'damaged'])
def test_unrecorded_fri_post_stays_unavailable(monkeypatch, tmp_path, record):
    """A Fri post neither Gene nor the notes record knows (a reply Gene has pruned, say) behaves as before."""
    async def scenario():
        fri, agent, store, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        old = fri_post(message, 'an old fri post nobody tracked', 20)
        if record == 'elsewhere':
            posted(tmp_path, (1234, channel.id))
        elif record == 'other-channel':
            posted(tmp_path, (old.id, 2))
        elif record == 'damaged':
            (tmp_path/'patch-notes-server.json').write_text(json.dumps({'versions': {'1.0.0': {'parts': [
                {'message_id': str(old.id), 'channel_id': '1'}]}}}))
        event = message('fri what was that', user=40)
        event.reference = SimpleNamespace(message_id=old.id, channel_id=channel.id)
        fri.chat_once.return_value = ChatResult(status='completed', reply='no idea, that one fell out of my head')
        try:
            context = await answered(agent, fri, event)
            assert context['reply_target'][0]['text'] == UNAVAILABLE and context['unattributed_reference']
            assert store.chat_context('123', 'public:1', '40')['turns'][-1]['reply'] == UNATTRIBUTED
        finally:
            await fri.close()
    asyncio.run(scenario())
