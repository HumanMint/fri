"""Deleted and edited messages in Fri's live view, offline and against the real controller."""

import asyncio
import json
import logging
import time
from collections import Counter
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


def test_a_deletion_mark_stays_while_its_message_is_handled_however_many_deletes_follow(monkeypatch):
    # The record is bounded, but a fence still waiting on a deleted message must keep seeing it gone.
    monkeypatch.setattr('fri.fri_agent.MAX_DELIVERED', 3)
    monkeypatch.setattr('fri.fri_agent.REDELIVERY_SECONDS', 0)
    agent = bare_agent()
    agent.recent[1].extend(row(i) for i in range(1, 9))
    agent.handling[50] += 1  # E.g. a turn paused before its model request.
    agent.replying.add(60)
    agent.unsend(1, [50, 60])
    for batch in ([1, 2], [3, 4], [5, 6], [7, 8]):  # Bulk deletes, each older mark past capacity and age.
        agent.unsend(1, batch)
    assert {50, 60} <= set(agent.unsent)
    assert len(agent.unsent) <= 3+2
    agent.handling -= Counter([50])
    agent.replying.discard(60)
    agent.unsend(1, [9])
    assert not {50, 60} & set(agent.unsent)  # Handled now: they go like any other.


def test_a_shown_messages_deletion_mark_outlasts_a_flood_of_unrelated_deletes(monkeypatch):
    # A nearby message the model saw isn't being handled, but the reply's last check still needs its mark.
    monkeypatch.setattr('fri.fri_agent.MAX_DELIVERED', 3)
    agent = bare_agent()
    agent.recent[1].extend(row(i) for i in range(1, 6))
    agent.unsend(1, [5])
    for start in range(1000, 1500, 100):  # Bulk deletes of messages Fri never had in view.
        agent.unsend(1, range(start, start+100))
    assert 5 in agent.unsent
    assert not any(i >= 1000 for i in agent.unsent)


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
def test_a_deleted_forget_me_is_not_carried_out(monkeypatch, tmp_path, waited):
    # Deleting it may be taking it back, and an erasure can't be undone: as its recheck before running.
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
            assert store.chat_context('123', 'public:1', '20')['personal_memory']['enabled']
            control.reply.assert_not_awaited()
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
        agent.run = AsyncMock(side_effect=lambda role, instruction, data, schema, **_: (
            {'action': 'reply', 'source_id': data['fresh_ids'][0]}, {}, 'completed'))
        agent.respond = AsyncMock()
        agent.recent[456].append(row(100, user='11', text='help? is it broken'))
        agent.recent[457].append(row(200, user='12', text=later.content))
        await agent.attend()  # The first channel's pick is gone; the second still gets its turn.
        agent.respond.assert_awaited_once_with(later, later.content, ambient=True)
    asyncio.run(scenario())


def test_a_partial_edit_drops_the_row_instead_of_crashing():
    agent = bare_agent()
    agent.recent[1].extend([row(1), row(2)])
    agent.edit(1, SimpleNamespace(id=1, content='half an update'))  # No author to check it against.
    assert [r['id'] for r in agent.recent[1]] == ['2']


def model(fri, tmp_path, respond):
    """Fri's own model call, with only the model replaced: `respond(context, ready)` runs its last checks."""
    from fri.fri import Fri
    fri.data = tmp_path
    fri.flush_usage = AsyncMock()
    async def signed_in(text, context, ready=None):
        return await respond(context, ready)
    async def call(text, context):
        return await Fri.chat_once(fri, text, context)
    fri.chat = SimpleNamespace(respond=signed_in)
    fri.chat_once = AsyncMock(side_effect=call)


async def delete(fri, rows, item):
    rows.pop(item.id, None)
    await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=item.id))


def no_debounce(monkeypatch):
    real_sleep = asyncio.sleep
    async def instant(*args, **kwargs):
        await real_sleep(0)
    monkeypatch.setattr('fri.fri_agent.asyncio.sleep', instant)


async def heard(agent, item):
    """A member's line in the live view, as Fri hears it."""
    await agent.handle(item)
    assert any(r['id'] == str(item.id) for r in agent.recent[1])


@pytest.mark.parametrize('when', ['before', 'after'])
def test_another_members_deleted_message_never_reaches_the_model_or_the_reply(monkeypatch, tmp_path, caplog, when):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('my landlord is ezra kim on 5th street lol', user=21)
        trigger = message('<@99> what did she just say')
        seen = []
        async def respond(context, ready):
            if when == 'before':  # Deleted after the live view was read, before the request.
                await delete(fri, rows, regret)
            await ready()
            seen.append([r['text'] for r in context.get('recent', [])])
            if when == 'after':  # Deleted while the model wrote its answer.
                await delete(fri, rows, regret)
            return ChatResult(status='completed', reply='she said her landlord is ezra kim')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, regret)
            await asyncio.wait_for(agent.handle(trigger), 10)
            if when == 'before':
                assert not any('ezra' in text for text in seen[0])
                trigger.reply.assert_awaited_once()  # Answered without it.
            else:
                assert any('ezra' in text for text in seen[0])
                trigger.reply.assert_awaited_once()
                assert trigger.reply.await_args.args[0] == 'things changed while i was typing. ping me again and i’ll catch up.'
                assert not store.chat_context('123', 'public:1', '20')['turns']
                assert f'Mention 1/{trigger.id} not answered, told why: nearby message deleted' in caplog.messages
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_message_deleted_while_typing_starts_never_reaches_the_model(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        trigger = message('fri my ssn is 078-05-1120 can you remember it')
        asked = []
        async def respond(context, ready):
            await ready()
            asked.append(context)  # The request would go out here.
            return ChatResult(status='completed', reply='noted')
        model(fri, tmp_path, respond)
        class Typing:  # Entering Discord's typing context awaits a request; the delete lands meanwhile.
            async def __aenter__(self):
                await delete(fri, rows, trigger)
            async def __aexit__(self, *exc):
                pass
        channel.typing = Typing
        def fetch(mid):  # As Discord answers for a deleted message.
            if mid not in rows:
                raise GONE
            return rows[mid]
        channel.fetch_message.side_effect = fetch
        try:
            await asyncio.wait_for(agent.handle(trigger), 10)
            assert not asked  # A ping's invitation already refetches it before the request; a name has this.
            trigger.reply.assert_not_awaited()
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reaction_queued_behind_another_is_not_saved_once_its_message_is_deleted(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        def fetch(mid):
            if mid not in rows:
                raise GONE
            return rows[mid]
        channel.fetch_message = AsyncMock(side_effect=fetch)
        trigger = message('fri lmaooo')
        trigger.reactions, trigger.add_reaction = [], AsyncMock()
        fri.chat_once.return_value = ChatResult(status='completed', response_kind='react', reaction='😭')
        record = AsyncMock(wraps=fri.record_turn)
        monkeypatch.setattr(fri, 'record_turn', record)
        await agent.reaction_lock.acquire()  # Another reaction is going out.
        task = asyncio.create_task(agent.handle(trigger))
        try:
            for _ in range(200):
                if fri.chat_once.await_count:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            await delete(fri, rows, trigger)
            agent.reaction_lock.release()
            await asyncio.wait_for(task, 10)
            trigger.add_reaction.assert_not_awaited()
            record.assert_not_awaited()
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            if agent.reaction_lock.locked():
                agent.reaction_lock.release()
            await asyncio.gather(task, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_deleted_messages_never_come_back_into_the_live_view(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('ok fine i cheated on the quiz', user=21)
        incoming = agent.incoming
        async def slow_profile(item, *args, **kwargs):  # Deleted while its author's profile is read.
            profile = await incoming(item, *args, **kwargs)
            if item is regret:
                await delete(fri, rows, regret)
            return profile
        agent.incoming = slow_profile
        trigger = message('fri hey')
        sent = SimpleNamespace(id=trigger.id+100, content='that meme is doing a lot of work')
        trigger.reply = AsyncMock(return_value=sent)
        record_turn = fri.record_turn
        async def record(result, delivered=None):  # Fri's reply deleted, e.g. by a mod, while it saves.
            if delivered is sent:
                await fri.on_raw_message_delete(SimpleNamespace(channel_id=1, message_id=sent.id))
            return await record_turn(result, delivered)
        monkeypatch.setattr(fri, 'record_turn', record)
        try:
            await agent.handle(regret)
            await asyncio.wait_for(agent.handle(trigger), 10)
            trigger.reply.assert_awaited_once()
            assert {r['id'] for r in agent.recent[1]} == {str(trigger.id)}
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_message_the_model_saw_then_pushed_out_of_the_live_view_still_withholds_the_reply(monkeypatch, tmp_path):
    """fri #67, #88: the live view keeps 30 rows; a row the request carried is marked when deleted even once it's gone."""
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('my landlord is ezra kim on 5th street lol', user=21)
        trigger = message('<@99> what did she just say')
        async def respond(context, ready):
            await ready()
            assert any('ezra' in r['text'] for r in context.get('recent', []))
            for n in range(30):  # A busy channel pushes it out of the live view while the model writes.
                agent.recent[1].append(row(10**6+n, user='22'))
            assert not any(r['id'] == str(regret.id) for r in agent.recent[1])
            await delete(fri, rows, regret)
            return ChatResult(status='completed', reply='she said her landlord is ezra kim')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, regret)
            await asyncio.wait_for(agent.handle(trigger), 10)
            trigger.reply.assert_awaited_once()
            assert 'ezra' not in trigger.reply.await_args.args[0]
            assert not store.chat_context('123', 'public:1', '20')['turns']
            assert regret.id in agent.unsent
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_reply_target_deleted_during_the_last_checks_never_reaches_the_model(monkeypatch, tmp_path):
    """fri #48: what a turn replies to is carried like the live view's rows, so a delete while Gene's check
    runs (after the reply target was refetched) is marked and stops the request."""
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        fetch = channel.fetch_message.side_effect
        async def fetch_or_gone(mid):
            if mid not in rows:
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
            return await fetch(mid) if asyncio.iscoroutinefunction(fetch) else fetch(mid)
        channel.fetch_message.side_effect = fetch_or_gone
        regret = message('my landlord is ezra kim on 5th street lol', user=21, age=3000)  # Never in the live view.
        trigger = message('<@99> what does this say')
        trigger.reference = SimpleNamespace(message_id=regret.id, channel_id=1)
        api, seen = fri.api, []
        async def delete_during_check(method, path, payload=None):
            result = await api(method, path, payload)
            if path == '/chat/turn/check' and regret.id in rows and not seen:
                await delete(fri, rows, regret)
            return result
        fri.api = delete_during_check
        async def respond(context, ready):
            await ready()
            seen.append(json.dumps(context, default=str))
            return ChatResult(status='completed', reply='it says her landlord is ezra kim')
        model(fri, tmp_path, respond)
        try:
            await asyncio.wait_for(agent.handle(trigger), 10)
            assert not seen  # The request carrying it never went out.
            assert all('ezra' not in str(call) for call in trigger.reply.await_args_list)
            assert regret.id not in rows and regret.id in agent.unsent
            assert not store.chat_context('123', 'public:1', '20')['turns']
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('address', ['fri', '<@99>'])
@pytest.mark.parametrize('deleted_at', ['before_ready', 'turn_check'])
def test_a_nearby_row_dropped_before_the_request_doesnt_withhold_an_unrelated_answer(monkeypatch, tmp_path, address, deleted_at):
    """Only what a request carries counts at delivery: a row deleted before it went out was never shown."""
    async def scenario():
        fri, agent, store, _, _, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        regret = message('unrelated nearby chatter', user=21)
        trigger = message(address + ' what is 2 plus 2?')
        requests, deleted = [], []
        api = fri.api
        async def delete_during_check(method, path, payload=None):
            result = await api(method, path, payload)
            if deleted_at == 'turn_check' and path == '/chat/turn/check' and not deleted:
                deleted.append(True)
                await delete(fri, rows, regret)
            return result
        fri.api = delete_during_check
        async def respond(context, ready):
            if deleted_at == 'before_ready' and not deleted:
                deleted.append(True)
                await delete(fri, rows, regret)
            await ready()
            requests.append([r['id'] for r in context.get('recent', [])])
            return ChatResult(status='completed', reply='2 plus 2 is 4')
        model(fri, tmp_path, respond)
        try:
            await heard(agent, regret)
            await asyncio.wait_for(agent.handle(trigger), 10)
            assert requests and str(regret.id) not in requests[-1] and regret.id in agent.unsent
            assert [call.args[0] for call in trigger.reply.await_args_list] == ['2 plus 2 is 4']
        finally:
            await fri.close()
    asyncio.run(scenario())
