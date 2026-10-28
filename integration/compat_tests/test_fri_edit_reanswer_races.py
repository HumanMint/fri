"""Races around answering an edited message again (sol-loop round 1 of fri #126, S1 to S3)."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from compat_tests.test_fri_mentions import environment
from compat_tests.test_fri_unsend import no_debounce
from compat_tests.test_fri_withdrawn_rows import model as attention_model
from fri.fri_chat import ChatResult

MARKER = 'synthetic-withdrawn-private-126'

async def drain_reruns(agent):
    while getattr(agent, 'reruns', None):
        await asyncio.wait_for(asyncio.gather(*agent.reruns), 4)


def test_attention_cannot_send_old_copy_after_edit_rerun_reinserts_same_id(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        fri.data = tmp_path
        trigger = message('fri explain ' + MARKER)
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='okay', edit=AsyncMock())
        requested, finish = asyncio.Event(), asyncio.Event()
        async def answer(text, context):
            if MARKER in text:
                requested.set()
                await finish.wait()
                edited = copy.copy(trigger)
                edited.content = 'fri explain safe albums'
                edited.edited_at = discord.utils.utcnow()
                edited.reply = AsyncMock(return_value=SimpleNamespace(id=trigger.id+101, content='okay', edit=AsyncMock()))
                rows[trigger.id] = edited
                await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=edited))
            return ChatResult(status='completed', reply='okay')
        fri.chat_once.side_effect = answer
        requests = []
        attention_model(fri, requests, attention=True)
        signin, release = asyncio.Event(), asyncio.Event()
        async def headers():
            signin.set()
            await release.wait()
            return {'Authorization': 'Bearer synthetic-review'}
        fri.chat.headers = headers
        task = asyncio.create_task(agent.handle(trigger))
        attention = None
        try:
            await asyncio.wait_for(requested.wait(), 2)
            # Direct rows are context for attention considering somebody else's fresh question.
            await agent.handle(message('anyone know why the build is broken?', user=30))
            attention = asyncio.create_task(agent.attend())
            await asyncio.wait_for(signin.wait(), 2)
            finish.set()
            await asyncio.wait_for(task, 4)
            await drain_reruns(agent)
            release.set()
            await asyncio.wait_for(attention, 4)
            assert MARKER not in json.dumps(requests), 'attention sent withdrawn old copy after edited row was reinserted'
        finally:
            release.set()
            finish.set()
            for t in [task, attention]:
                if t and not t.done():
                    t.cancel()
                    await asyncio.gather(t, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_rerun_cannot_join_newer_message_burst_in_reverse_id_order(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        original = message('fri explain the typo')
        original.reply.return_value = SimpleNamespace(id=original.id+100, content='okay', edit=AsyncMock())
        edited = copy.copy(original)
        edited.content = 'fri explain the album'
        edited.edited_at = discord.utils.utcnow()
        edited.reply = AsyncMock(return_value=SimpleNamespace(id=original.id+101, content='okay', edit=AsyncMock()))
        async def answer(text, context):
            if 'typo' in text:
                rows[original.id] = edited
                await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=edited))
            return ChatResult(status='completed', reply='actual answer')
        fri.chat_once.side_effect = answer
        fetch_wait, fetch_release = asyncio.Event(), asyncio.Event()
        async def fetch(mid):
            # Only the rerun's refetch runs with no handler holding this message.
            if mid == original.id and not agent.handling[mid] and getattr(agent, 'reanswered', {}).get(mid):
                fetch_wait.set()
                await fetch_release.wait()
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        new_wait, new_release = asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep
        async def sleep(delay, *args, **kwargs):
            if delay == 1.5 and asyncio.current_task().get_name() == 'new-message':
                new_wait.set()
                await new_release.wait()
            else:
                await real_sleep(0)
        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', sleep)
        task = None
        try:
            await agent.handle(original)
            if hasattr(agent, 'reruns'):
                await asyncio.wait_for(fetch_wait.wait(), 2)
            newer = message('fri and what about jazz?')
            newer.reply.return_value = SimpleNamespace(id=newer.id+100, content='okay', edit=AsyncMock())
            task = asyncio.create_task(agent.handle(newer), name='new-message')
            await asyncio.wait_for(new_wait.wait(), 2)
            fetch_release.set()
            await drain_reruns(agent)
            new_release.set()
            await asyncio.wait_for(task, 2)
            assert any('what about jazz' in call.args[0] for call in fri.chat_once.await_args_list), (
                'newer message was swallowed by reversed rerun burst: ' + str(edited.reply.await_args_list))
        finally:
            fetch_release.set()
            new_release.set()
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_edit_during_rerun_refetch_cannot_publish_stale_row_to_other_turn(monkeypatch, tmp_path):
    async def scenario():
        from fri.fri import Fri
        fri, agent, store, _, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=1)
        fri.data = tmp_path
        fri.flush_usage = AsyncMock()
        no_debounce(monkeypatch)
        trigger = message('fri explain the typo')
        trigger.reply.return_value = SimpleNamespace(id=trigger.id+100, content='okay', edit=AsyncMock())
        first_edit, requested = [], []
        async def edit_to(item, text):
            edited = copy.copy(item)
            edited.content, edited.edited_at = text, discord.utils.utcnow()
            edited.reply = AsyncMock(return_value=SimpleNamespace(id=item.id+101, content='okay', edit=AsyncMock()))
            rows[item.id] = edited
            await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=edited))
            return edited
        async def respond(text, context, ready):
            await ready()
            requested.append(text + json.dumps(context.get('recent', [])))
            if 'typo' in text:
                first_edit.append(await edit_to(trigger, 'fri explain ' + MARKER))
            return ChatResult(status='completed', reply='okay')
        fri.chat = SimpleNamespace(respond=respond)
        async def call(text, context):
            return await Fri.chat_once(fri, text, context)
        fri.chat_once = AsyncMock(side_effect=call)
        reread, reread_release = asyncio.Event(), asyncio.Event()
        refetched = False
        async def fetch(mid):
            nonlocal refetched
            item = rows[mid]
            if mid == trigger.id and first_edit:
                if not agent.handling[mid] and not refetched:
                    refetched = True
                    # REST has read edit 1; edit 2 lands over the gateway before its response returns.
                    await edit_to(item, 'fri explain safe albums')
                    return item
                if refetched and agent.handling[mid] and not reread.is_set():
                    # The rerun already exposed its row; its own pre-model refetch is still waiting.
                    reread.set()
                    await reread_release.wait()
            return item
        channel.fetch_message.side_effect = fetch
        try:
            await agent.handle(trigger)
            if hasattr(agent, 'reruns'):
                await asyncio.wait_for(reread.wait(), 2)
            later = message('fri analyze the discussion', user=30)
            later.reply.return_value = SimpleNamespace(id=later.id+100, content='okay', edit=AsyncMock())
            await agent.handle(later)
            reread_release.set()
            await drain_reruns(agent)
            assert MARKER not in str(requested), 'another member model call got the refetch-raced withdrawn text'
        finally:
            reread_release.set()
            await drain_reruns(agent)
            await fri.close()
    asyncio.run(scenario())
