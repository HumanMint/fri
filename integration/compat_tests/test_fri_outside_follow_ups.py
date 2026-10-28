"""NX1: a direct reply to Fri's own answer outside its channels reads that answer, while all it drew on stands."""

import asyncio
import contextlib
import copy
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from compat_tests.test_fri_mentions import environment
from fri.fri import UNAVAILABLE, Fri
from fri.fri_agent import Agent
from fri.fri_chat import ChatResult
from fri.fri_errors import ControllerRejected
from fri.fri_mentions import CONTEXT_CHANGED

OLD = 'the cat one is mine btw'
ANSWER = 'kev’s, the cat one. the other one is mid'
AGAIN = 'cause the cat has range. keep up'


def deleted():
    return discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'deleted')


def world(monkeypatch, tmp_path, *, channel_id=8):
    fri, agent, store, controller, channel, message, calls, rows = environment(monkeypatch, tmp_path, channel_id=channel_id)
    async def fetch(mid):
        if mid not in rows:
            raise deleted()
        return rows[mid]
    channel.fetch_message.side_effect = fetch
    clock = [0.0]  # Seconds added to the invitation module's monotonic clock: a receipt's lifetime.
    monkeypatch.setattr('fri.fri_mentions.time', SimpleNamespace(time=time.time, monotonic=lambda: time.monotonic()+clock[0]))

    def fri_post(text, age=0):
        post = message(text, user=99, age=age)
        post.author.bot = True
        return post
    return SimpleNamespace(fri=fri, agent=agent, store=store, controller=controller, channel=channel, message=message,
                           calls=calls, rows=rows, fri_post=fri_post, clock=clock)


def reply_to(event, target):
    event.reference = SimpleNamespace(message_id=target.id, channel_id=target.channel.id)
    return event


async def handled(w, event, reply, *, age=0):
    """Fri answers `event` with `reply`: the context the model got, and Fri's delivered post."""
    w.fri.chat_once.reset_mock()
    w.fri.chat_once.return_value = ChatResult(status='completed', reply=reply)
    event.reply.return_value = sent = w.fri_post(reply, age)
    with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
        await w.agent.handle(event)
    context = w.fri.chat_once.await_args.args[1] if w.fri.chat_once.await_args else None
    return context, sent


async def opened(w):
    """Someone's post outside Fri's channels (old enough not to be nearby), a ping replying to it, Fri's answer."""
    old = w.message(OLD, user=30, age=7*3600)
    ping = reply_to(w.message('<@99> which one is better', user=20, age=120), old)
    context, answer = await handled(w, ping, ANSWER, age=110)
    assert context['invited_context'][0]['text'] == OLD  # The first answer drew on it.
    ping.reply.assert_awaited_once()
    return old, ping, answer


def own(context):
    return [row for row in context.get('invited_context', []) if row.get('fri')]


def tracked(w, event):
    """The authors the follow-up's mention grant fences on: Gene's scoped privacy record for it."""
    with w.store.connection() as db:
        row = db.execute('SELECT profiles FROM mention_grants WHERE source_id=?', (str(event.id),)).fetchone()
    return set(json.loads(row[0])) if row else set()


def follow_up(w, target, *, user=40, age=30):
    return reply_to(w.message('<@99> why that one tho', user=user, age=age), target)


# Fail on main: the answer is never read.

def test_a_reply_to_fris_outside_answer_reads_that_answer(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, ping, answer = await opened(w)
            event = follow_up(w, answer)
            context, _ = await handled(w, event, AGAIN)
            rows = own(context)
            assert [row['text'] for row in rows] == [ANSWER]
            assert rows[0]['referenced'] and rows[0]['source']['id'] == str(answer.id)
            assert rows[0]['source']['user_id'] == '99' and 'privacy_revision' not in rows[0]
            # Fri's own words, never the post they drew on: that stays where the first invitation read it.
            assert OLD not in json.dumps(context['invited_context'])
            assert [row['source']['id'] for row in context['invited_context'] if not row.get('fri')] == [str(ping.id)]
            # Every author the answer drew on is fenced by this ping's grant at Gene, like a nearby author.
            assert {'20', '30', '40'} <= tracked(w, event)
            assert event.reply.await_args.args[0] == AGAIN
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_two_follow_up_generations_read_the_newest_answer(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, _, answer = await opened(w)
            _, second = await handled(w, follow_up(w, answer, age=60), AGAIN, age=50)
            event = follow_up(w, second, user=41)
            context, _ = await handled(w, event, 'still the cat')
            assert [row['text'] for row in own(context)] == [AGAIN]
            assert {'20', '30', '40', '41'} <= tracked(w, event)  # The whole ancestry, never truncated.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('when', ['refetch', 'model'])
def test_an_opt_out_by_an_author_the_answer_drew_on_stops_the_follow_up(monkeypatch, tmp_path, when):
    """Checked again before the model and before delivery, as a nearby author's would be."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            old, _, answer = await opened(w)
            event = follow_up(w, answer)
            fetch, fetches = w.channel.fetch_message.side_effect, []
            async def watched(mid):
                if mid == old.id:
                    fetches.append(mid)
                    if when == 'refetch' and len(fetches) == 2:  # Collect's read, then its last check's.
                        w.store.personal_control('30', 'disable')
                return await fetch(mid)
            w.channel.fetch_message.side_effect = watched
            async def respond(_, context):
                assert [row['text'] for row in own(context)] == [ANSWER]
                if when == 'model':
                    w.store.personal_control('30', 'disable')
                return ChatResult(status='completed', reply='the cat, obviously')
            w.fri.chat_once.reset_mock()
            w.fri.chat_once.side_effect = respond
            event.reply.return_value = w.fri_post('the cat, obviously')
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await w.agent.handle(event)
            assert [item.args[0] for item in event.reply.await_args_list] == [CONTEXT_CHANGED]
            assert w.fri.chat_once.await_count == (1 if when == 'model' else 0)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_the_receipt_store_keeps_the_newest_hundred():
    from fri.fri_mentions import MAX_RECEIPTS, RECEIPT_SECONDS, Receipts
    assert (MAX_RECEIPTS, RECEIPT_SECONDS) == (100, 15*60)
    receipts = Receipts()
    for mid in range(1, 102):
        receipts.keep(mid, {'channel': 8, 'version': 'v', 'sources': {}, 'authors': {}, 'until': time.monotonic()+60})
    assert receipts.get(1) is None and all(receipts.get(mid) for mid in range(2, 102))


# Guards: each passes on main, and must still behave as today.

@pytest.mark.parametrize('elapsed', [15*60+1, 3600])
def test_an_answer_older_than_fifteen_minutes_is_read_as_before(monkeypatch, tmp_path, elapsed):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, _, answer = await opened(w)
            w.clock[0] = elapsed
            context, _ = await handled(w, follow_up(w, answer), AGAIN)
            assert not own(context) and ANSWER not in json.dumps(context['invited_context'])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_follow_up_cannot_outlive_the_answer_it_inherits(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, _, answer = await opened(w)
            w.clock[0] = 10*60
            _, second = await handled(w, follow_up(w, answer, age=60), AGAIN, age=50)
            w.clock[0] = 15*60+1  # Five minutes after the second answer, past the first one's fifteen.
            context, _ = await handled(w, follow_up(w, second, user=41), 'still the cat')
            assert not own(context)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_answers_past_the_cap_are_read_as_before(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        monkeypatch.setattr('fri.fri_mentions.MAX_RECEIPTS', 1, raising=False)
        try:
            _, _, first = await opened(w)
            other = w.message('<@99> and this one?', user=21, age=100)
            _, second = await handled(w, other, 'nah', age=90)
            context, _ = await handled(w, follow_up(w, first), AGAIN)
            assert not own(context)  # Pushed out by the newer answer.
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bound', ['MAX_EVIDENCE', 'MAX_AUTHORS'])
def test_an_answer_past_one_privacy_reads_bounds_is_read_as_before(monkeypatch, tmp_path, bound):
    """The answer's two posts and two authors fit, but not with a newer post by someone else: never cut short."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        monkeypatch.setattr('fri.fri_mentions.'+bound, 2, raising=False)
        try:
            _, _, answer = await opened(w)
            w.message('lmao the cat', user=31, age=60)
            event = follow_up(w, answer)
            context, _ = await handled(w, event, AGAIN)
            assert not own(context) and '30' not in tracked(w, event)  # Nothing of the answer was read.
            assert [row['text'] for row in context['invited_context']] == ['<@99> which one is better', 'lmao the cat']
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_reply_from_another_channel_is_read_as_before(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, _, answer = await opened(w)
            elsewhere = copy.copy(w.channel)
            elsewhere.id = 9
            elsewhere.fetch_message = AsyncMock(side_effect=w.channel.fetch_message.side_effect)  # Even if it resolved.
            channels = {8: w.channel, 9: elsewhere}
            w.channel.guild.fetch_channel = AsyncMock(side_effect=lambda cid: channels[cid])
            w.fri.get_channel = channels.get
            for reference in (9, None):
                event = w.message('<@99> why that one tho', user=40, age=30)
                event.channel = elsewhere
                event.reference = SimpleNamespace(message_id=answer.id, channel_id=reference)
                context, _ = await handled(w, event, AGAIN)
                assert not own(context) and ANSWER not in json.dumps(context['invited_context'])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('raw', [True, False])  # Whether Discord's delete or edit event reached Fri.
@pytest.mark.parametrize('change', ['delete', 'edit'])
@pytest.mark.parametrize('target', ['answer', 'ping', 'old'])
def test_an_answer_whose_posts_changed_is_read_as_before(monkeypatch, tmp_path, target, change, raw):
    """Fri's answer deleted or edited, or a post it drew on deleted or edited, by anyone."""
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            old, ping, answer = await opened(w)
            post = {'answer': answer, 'ping': ping, 'old': old}[target]
            if change == 'delete':
                del w.rows[post.id]
                if raw:
                    w.agent.unsend(w.channel.id, [post.id])
            else:
                edited = copy.copy(post)
                edited.content, edited.edited_at = post.content+' (nvm)', dt.datetime.now(dt.timezone.utc)
                w.rows[post.id] = edited
                if raw:
                    w.agent.edit(w.channel.id, edited)
            context, _ = await handled(w, follow_up(w, answer), AGAIN)
            assert not own(context) and ANSWER not in json.dumps(context['invited_context'])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('raw', [True, False])
def test_a_post_the_first_answer_drew_on_changed_stops_the_second_generation(monkeypatch, tmp_path, raw):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            old, _, answer = await opened(w)
            _, second = await handled(w, follow_up(w, answer, age=60), AGAIN, age=50)
            edited = copy.copy(old)
            edited.content, edited.edited_at = 'actually the dog one', dt.datetime.now(dt.timezone.utc)
            w.rows[old.id] = edited
            if raw:
                w.agent.edit(w.channel.id, edited)
            context, _ = await handled(w, follow_up(w, second, user=41), 'still the cat')
            assert not own(context)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('author', ['20', '30'])
@pytest.mark.parametrize('control', ['disable', 'erase'])
def test_an_answer_drawing_on_someone_who_opted_out_is_read_as_before(monkeypatch, tmp_path, author, control):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, _, answer = await opened(w)
            w.store.erase_user(author) if control == 'erase' else w.store.personal_control(author, 'disable')
            context, _ = await handled(w, follow_up(w, answer), AGAIN)
            assert not own(context) and ANSWER not in json.dumps(context['invited_context'])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('target', ['member', 'untracked fri post'])
def test_a_reply_to_anything_else_is_read_as_before(monkeypatch, tmp_path, target):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            old, _, _ = await opened(w)
            post = old if target == 'member' else w.fri_post('an old fri post nobody tracked', 100)
            context, _ = await handled(w, follow_up(w, post), AGAIN)
            assert not own(context)
            referenced = [row for row in context['invited_context'] if row['referenced']]
            assert [row['text'] for row in referenced] == ([OLD] if target == 'member' else [])
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_restart_forgets_every_answer(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            _, _, answer = await opened(w)
            restarted = w.fri.agent = Agent(w.fri)
            restarted.state, restarted.record = w.agent.state, AsyncMock()
            w.agent = restarted
            context, _ = await handled(w, follow_up(w, answer), AGAIN)
            assert not own(context)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['send', 'save', 'deleted while saving'])
def test_an_answer_that_was_not_delivered_and_saved_is_read_as_before(monkeypatch, tmp_path, failure):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            old = w.message(OLD, user=30, age=7*3600)
            ping = reply_to(w.message('<@99> which one is better', user=20, age=120), old)
            answer, api = w.fri_post(ANSWER, 110), w.fri.api
            if failure == 'send':  # A timeout that may have landed all the same.
                ping.reply.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason='timeout'), 'x')
            else:
                async def saving(method, path, payload=None):
                    if path == '/chat/turn':
                        if failure == 'save':
                            raise ControllerRejected('refused')
                        del w.rows[old.id]  # The post it drew on, taken back as the save went in.
                        w.agent.unsend(w.channel.id, [old.id])
                    return await api(method, path, payload)
                w.fri.api = saving
                ping.reply.return_value = answer
            w.fri.chat_once.return_value = ChatResult(status='completed', reply=ANSWER)
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()), contextlib.suppress(discord.HTTPException):
                await w.agent.handle(ping)
            w.fri.api = api
            context, _ = await handled(w, follow_up(w, answer), AGAIN)
            assert not own(context)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_configured_channel_answer_makes_no_receipt(monkeypatch, tmp_path):
    """Fri's answer in its own channel, then that channel dropped from them: a reply to it reads as before."""
    async def scenario():
        w = world(monkeypatch, tmp_path, channel_id=1)
        try:
            ping = w.message('<@99> which one is better', user=20, age=120)
            _, answer = await handled(w, ping, ANSWER, age=110)
            ping.reply.assert_awaited_once()
            w.agent.state = {**w.agent.state, 'channels': []}
            context, _ = await handled(w, follow_up(w, answer), AGAIN)
            assert not own(context)
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_configured_channel_ping_reads_fris_answer_as_before(monkeypatch, tmp_path):
    """An outside answer, then its channel added to Fri's: the configured path's reply target, as on main."""
    async def scenario():
        w = world(monkeypatch, tmp_path, channel_id=1)
        configured = w.agent.state
        w.agent.state = {**configured, 'channels': []}
        try:
            ping = w.message('<@99> which one is better', user=20, age=120)
            _, answer = await handled(w, ping, ANSWER, age=110)
            w.agent.state = configured
            context, _ = await handled(w, follow_up(w, answer), AGAIN)
            assert not own(context)
            assert [row['text'] for row in context['reply_target']] == [UNAVAILABLE]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_a_dm_reads_no_invited_context(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            await opened(w)
            w.agent.config['support_dms'] = True
            w.agent.member = AsyncMock(return_value=SimpleNamespace(id=40))
            event = w.message('why that one tho', user=40)
            event.guild = None
            context, _ = await handled(w, event, AGAIN)
            assert 'invited_context' not in context and ANSWER not in json.dumps(context, default=str)
            assert event.reply.await_args.args[0] == AGAIN
        finally:
            await w.fri.close()
    asyncio.run(scenario())


# Review round 1: a withdrawal or expiry during a check's last wait still stops the answer reaching the model or a reply.

def taking_back(w, target, post, active):
    """Its API, with `post` taken back (`target`: how) during the second privacy read of a check while `active`."""
    api, reads, done = w.fri.api, [0], [False]

    async def watched(method, path, payload=None):
        result = await api(method, path, payload)
        if active[0] and path == '/chat/mentions/profiles':
            reads[0] += 1
            if reads[0] == 2:  # The check's last wait, after every refetch.
                if target == 'delete':
                    w.rows.pop(post.id)
                    w.agent.unsend(w.channel.id, [post.id])
                else:
                    changed = copy.copy(post)
                    changed.content = 'TAKEN BACK'
                    changed.edited_at = dt.datetime.now(dt.timezone.utc)
                    w.rows[post.id] = changed
                    w.agent.edit(w.channel.id, changed)
                done[0] = True
        return result
    return watched, done


@pytest.mark.parametrize('target', ['old-delete', 'old-edit', 'answer-edit', 'answer-delete'])
@pytest.mark.parametrize('phase', ['model', 'delivery'])
def test_a_withdrawal_during_the_last_check_stops_the_answer(monkeypatch, tmp_path, target, phase):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        w.fri.data = tmp_path
        try:
            old, _, answer = await opened(w)
            event = follow_up(w, answer)
            event.reply.return_value = w.fri_post(AGAIN)
            active, shown = [False], []
            post = answer if target.startswith('answer') else old
            w.fri.api, done = taking_back(w, target.split('-')[1], post, active)

            async def model(text, context, *, ready=None):
                active[0] = phase == 'model'
                await ready()
                shown.extend(own(context))
                active[0] = phase == 'delivery'
                return ChatResult(status='completed', reply=AGAIN)
            w.fri.chat_once = Fri.chat_once.__get__(w.fri, Fri)
            w.fri.chat = SimpleNamespace(respond=model)
            w.fri.flush_usage = AsyncMock()
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await w.agent.handle(event)
            assert done[0]
            if phase == 'model':
                assert not shown  # Refused before the request.
            assert AGAIN not in [item.args[0] for item in event.reply.await_args_list]
            assert w.agent.receipts.get(answer.id) is None
        finally:
            await w.fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['model', 'delivery'])
def test_an_answer_expiring_while_fri_replies_stops_the_reply(monkeypatch, tmp_path, phase):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        w.fri.data = tmp_path
        try:
            _, _, answer = await opened(w)
            w.clock[0] = 15*60-1
            event = follow_up(w, answer)
            event.reply.return_value = w.fri_post(AGAIN)
            shown = []

            async def model(text, context, *, ready=None):
                if phase == 'model':
                    w.clock[0] = 15*60+1
                await ready()
                shown.extend(own(context))
                w.clock[0] = 15*60+1
                return ChatResult(status='completed', reply=AGAIN)
            w.fri.chat_once = Fri.chat_once.__get__(w.fri, Fri)
            w.fri.chat = SimpleNamespace(respond=model)
            w.fri.flush_usage = AsyncMock()
            with patch('fri.fri_agent.asyncio.sleep', new=AsyncMock()):
                await w.agent.handle(event)
            assert bool(shown) == (phase == 'delivery')  # In date when the request went out.
            assert AGAIN not in [item.args[0] for item in event.reply.await_args_list]
        finally:
            await w.fri.close()
    asyncio.run(scenario())


def test_an_answers_fifteen_minutes_start_when_it_goes_out(monkeypatch, tmp_path):
    async def scenario():
        w = world(monkeypatch, tmp_path)
        try:
            old = w.message(OLD, user=30, age=7*3600)
            event = reply_to(w.message('<@99> which one is better', age=120), old)
            api = w.fri.api

            async def saving(method, path, payload=None):
                if path == '/chat/turn':
                    w.clock[0] = 30  # A slow save, after delivery.
                return await api(method, path, payload)
            w.fri.api = saving
            _, answer = await handled(w, event, ANSWER, age=110)
            w.clock[0] = 15*60-1
            assert w.agent.receipts.get(answer.id) is not None
            w.clock[0] = 15*60+1  # Fifteen minutes from delivery, not from the save.
            assert w.agent.receipts.get(answer.id) is None
        finally:
            await w.fri.close()
    asyncio.run(scenario())
