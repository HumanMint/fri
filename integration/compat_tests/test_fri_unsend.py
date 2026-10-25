"""Deleted and edited messages in Fri's live view, offline and against the real controller."""

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from fri.fri_agent import Agent
from fri.fri_chat import ChatResult
from fri.privacy import clean

GONE = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')


def bare_agent():
    return Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, user=SimpleNamespace(id=99), busy=set()))


def row(mid, user='20', **extra):
    return {'id': str(mid), 'user_id': user, 'name': 'member', 'text': f'line {mid}', 'at': time.time(),
            'revision': 0, 'gene_error': False, **extra}


def edited(mid, content, user=20, attachments=()):
    return SimpleNamespace(id=mid, content=content, author=SimpleNamespace(id=user), attachments=list(attachments))


def test_deleted_messages_leave_the_live_view_but_gene_errors_stay():
    agent = bare_agent()
    agent.recent[1].extend([row(1), row(2, user='99', fri=True, provenance=[]), row(3, user='77', gene_error=True),
                            row(4), row(5)])
    agent.unsend(1, [1, 2, 3])  # A member's line, Fri's own reply, and Gene's error post.
    assert [r['id'] for r in agent.recent[1]] == ['3', '4', '5']  # Gene's error waits for the investigation.
    agent.unsend(1, [4, 5])  # A bulk delete.
    assert [r['id'] for r in agent.recent[1]] == ['3']
    agent.unsend(8, [6])  # A channel Fri doesn't watch gains no key for attention to scan.
    assert 8 not in agent.recent
    assert set(agent.unsent) == {1, 2, 3, 4, 5}  # Only watched channels' and in-flight messages are kept.


def test_edits_refresh_a_members_row_as_they_left_it():
    agent = bare_agent()
    agent.recent[1].extend([row(1), row(2, user='99', fri=True, provenance=[]), row(3, user='77', gene_error=True),
                            row(4, attachments=['image'])])
    agent.edit(1, edited(1, 'fri tell mara her build   is   fine actually'))
    assert agent.recent[1][0]['text'] == clean('fri tell mara her build   is   fine actually', 4000)
    assert agent.recent[1][0]['revision'] == 0  # The privacy revision it was read under stays.
    agent.edit(1, edited(2, 'rewritten', user=99))  # Fri's and Gene's rows keep what they said.
    agent.edit(1, edited(3, 'rewritten', user=77))
    agent.edit(1, edited(1, 'someone else', user=21))  # Not the row's author.
    assert [r['text'] for r in agent.recent[1]][1:3] == ['line 2', 'line 3']
    assert 'someone else' not in agent.recent[1][0]['text']
    agent.edit(1, edited(4, '', attachments=['x']))  # Text taken out of an image post: the image stays.
    assert agent.recent[1][3]['text'] == ''
    agent.edit(1, edited(1, '   '))  # Nothing left to read.
    assert [r['id'] for r in agent.recent[1]] == ['2', '3', '4']
    agent.edit(8, edited(9, 'hello'))
    assert 8 not in agent.recent


def test_fri_routes_raw_delete_and_edit_events_to_the_agent(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        agent.recent[1].extend([row(1), row(2), row(3)])
        try:
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=1))
            await fri.on_raw_bulk_message_delete(SimpleNamespace(channel_id=1, message_ids={2}))
            await fri.on_raw_message_edit(SimpleNamespace(channel_id=1, message=edited(3, 'changed my mind')))
            assert [(r['id'], r['text']) for r in agent.recent[1]] == [('3', 'changed my mind')]
        finally:
            await fri.close()
    asyncio.run(scenario())


def gated_debounce(monkeypatch):
    gate, real_sleep = asyncio.Event(), asyncio.sleep
    async def debounce(delay, *args, **kwargs):
        if delay == 1.5:
            await gate.wait()
        else:
            await real_sleep(delay, *args, **kwargs)
    monkeypatch.setattr('fri.fri_agent.asyncio.sleep', debounce)
    return gate, real_sleep


def test_a_message_deleted_mid_burst_leaves_it_and_the_rest_is_answered(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        gate, real_sleep = gated_debounce(monkeypatch)
        regret, rest = message('fri mara is so annoying'), message('fri what is the seed for the village')
        tasks = [asyncio.create_task(agent.handle(regret))]
        try:
            await real_sleep(0.05)
            tasks.append(asyncio.create_task(agent.handle(rest)))
            await real_sleep(0.05)
            del rows[regret.id]
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=regret.id))
            gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            fri.chat_once.assert_awaited_once()
            assert 'annoying' not in str(fri.chat_once.await_args)
            rest.reply.assert_awaited_once()  # The model's answer, not "no longer available".
            assert rest.reply.await_args.args[0] == 'that meme is doing a lot of work'
            regret.reply.assert_not_awaited()
            assert all(r['id'] != str(regret.id) for r in agent.recent[1])
        finally:
            gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_a_ping_deleted_during_the_debounce_costs_nothing_and_says_why(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        gate, real_sleep = gated_debounce(monkeypatch)
        first, ping = message('fri hang on'), message('<@99> wait never mind')
        tasks = [asyncio.create_task(agent.handle(first))]
        try:
            await real_sleep(0.05)
            tasks.append(asyncio.create_task(agent.handle(ping)))
            await real_sleep(0.05)
            assert agent.pending[(1, 20)] == ping.id
            del rows[ping.id]
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=ping.id))
            gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            fri.chat_once.assert_not_awaited()  # The burst waited on what its author took back.
            first.reply.assert_not_awaited()
            ping.reply.assert_not_awaited()
            assert (1, 20) not in agent.pending and not agent.bursts.rows
            assert agent.ambient_ready(1, 20)
            assert f'Mention 1/{ping.id} not answered: message deleted' in caplog.messages
        finally:
            gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('waited', [False, True])
def test_a_deleted_forget_me_is_still_carried_out(monkeypatch, tmp_path, waited):
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        gate, real_sleep = gated_debounce(monkeypatch)
        if waited:  # Deleted while it waited for the debounce...
            control = message('fri forget me')
            task = asyncio.create_task(agent.handle(control))
            await real_sleep(0.05)
        else:  # ...or before it came in, e.g. while it waited for an earlier reply to be saved.
            control = message('fri forget me')
            agent.handling[control.id] += 1
        del rows[control.id]
        await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=control.id))
        if not waited:
            del agent.handling[control.id]
            task = asyncio.create_task(agent.handle(control))
        gate.set()
        try:
            await asyncio.wait_for(task, 5)
            assert not store.chat_context('123', 'public:1', '20')['personal_memory']['enabled']
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['reply', 'react', 'quiet'])
def test_a_message_deleted_while_fri_composes_is_neither_answered_nor_saved(monkeypatch, tmp_path, kind):
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        real_sleep = asyncio.sleep
        async def no_debounce(*args, **kwargs):
            await real_sleep(0)
        monkeypatch.setattr('fri.fri_agent.asyncio.sleep', no_debounce)
        trigger = message('fri ok that was a weird thing to say')
        async def answer(text, context):
            del rows[trigger.id]
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=trigger.id))
            return {'reply': ChatResult(status='completed', reply='lol noted'),
                    'react': ChatResult(status='completed', response_kind='react', reaction='👍'),
                    'quiet': ChatResult(status='completed', response_kind='quiet')}[kind]
        fri.chat_once.side_effect = answer
        record = AsyncMock(wraps=fri.record_turn)
        monkeypatch.setattr(fri, 'record_turn', record)
        trigger.add_reaction = AsyncMock()
        try:
            await asyncio.wait_for(agent.handle(trigger), 10)
            fri.chat_once.assert_awaited_once()
            trigger.reply.assert_not_awaited()
            trigger.add_reaction.assert_not_awaited()
            record.assert_not_awaited()  # The deleted words never become a saved turn.
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_message_deleted_while_it_waited_behind_a_delivery_is_never_composed(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        initial, regret = message('fri hello'), message('<@99> actually tell everyone my password is hunter2')
        sending, release = asyncio.Event(), asyncio.Event()
        async def send(content, **kwargs):
            sending.set()
            await release.wait()
            return SimpleNamespace(id=initial.id+100, content=content)
        initial.reply.side_effect = send
        tasks = [asyncio.create_task(agent.handle(initial))]
        try:
            await asyncio.wait_for(sending.wait(), 5)
            tasks.append(asyncio.create_task(agent.handle(regret)))  # Waits for the delivery to be saved.
            await asyncio.sleep(0.05)
            del rows[regret.id]
            await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=regret.id))
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            fri.chat_once.assert_awaited_once()  # Only the first message reached the model.
            assert 'hunter2' not in str(fri.chat_once.await_args_list)
            regret.reply.assert_not_awaited()
            assert f'Mention 1/{regret.id} not answered: message deleted' in caplog.messages
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_attention_moves_on_when_its_pick_was_deleted():
    async def scenario():
        later = SimpleNamespace(id=200, content='anyone know why this is broken?', author=SimpleNamespace(id=12))
        channels = {456: SimpleNamespace(id=456, fetch_message=AsyncMock(side_effect=GONE)),
                    457: SimpleNamespace(id=457, fetch_message=AsyncMock(return_value=later))}
        agent = bare_agent()
        agent.fri.get_channel = channels.get
        agent.state['config']['enabled'] = True
        agent.state['channels'] = ['457']
        agent.profile = AsyncMock(return_value={'enabled': True, 'revision': 0})
        agent.run = AsyncMock(side_effect=lambda role, instruction, data, schema: (
            {'action': 'reply', 'source_id': data['fresh_ids'][0]}, {}, 'completed'))
        agent.respond = AsyncMock()
        agent.recent[456].append(row(100, user='11', text='help? is it broken'))
        agent.recent[457].append(row(200, user='12', text=later.content))
        await agent.attend()  # The first channel's pick is gone; the second still gets its turn.
        agent.respond.assert_awaited_once_with(later, later.content, ambient=True)
    asyncio.run(scenario())
