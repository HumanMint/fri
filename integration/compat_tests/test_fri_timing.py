"""When Fri shows typing, how long a bare "fri" waits for the rest, and late ambient replies."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

from compat_tests.test_fri_mentions import environment
from fri.fri_agent import Agent, Typing
from fri.fri_chat import ChatResult


class Stop(Exception):
    pass


def record_typing(channel, log):
    @asynccontextmanager
    async def typing():
        log.append('typing')
        try:
            yield
        finally:
            log.append('stopped')
    channel.typing = typing


def no_debounce(monkeypatch):
    real_sleep = asyncio.sleep
    async def instant(*args, **kwargs):
        await real_sleep(0)
    monkeypatch.setattr('fri.fri_agent.asyncio.sleep', instant)
    return real_sleep


def answering(fri, log, *results):
    results = list(results)
    async def answer(text, context):
        log.append('model')
        return results.pop(0) if len(results) > 1 else results[0]
    fri.chat_once.side_effect = answer


def test_typing_is_cosmetic_and_starts_once():
    async def scenario():
        log = []
        channel = SimpleNamespace()
        record_typing(channel, log)
        async with Typing(channel, addressed=False) as typing:
            await typing('admitted')  # Nobody spoke to Fri: no words promised yet.
            assert log == []
            await typing('action')
            await typing('admitted')
        assert log == ['typing', 'stopped']
        refused = SimpleNamespace(typing=lambda: AsyncMock(__aenter__=AsyncMock(
            side_effect=discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no'))))
        async with Typing(refused, addressed=True) as typing:
            await typing('admitted')  # Refused: the reply still goes ahead.
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['fri what is the move tonight', '<@99> what is the move tonight'])
def test_a_turn_addressed_to_fri_types_once_admitted(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        log = []
        record_typing(channel, log)
        answering(fri, log, ChatResult(status='completed', reply='sleep, probably'))
        trigger = message(text)
        try:
            await asyncio.wait_for(agent.handle(trigger), 5)
            assert log == ['typing', 'model', 'stopped']
            trigger.reply.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_cooldown_never_types(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        log = []
        record_typing(channel, log)
        answering(fri, log, ChatResult(status='completed', reply='unused'))
        api = fri.api
        async def cooked(method, path, payload=None):
            if path == '/agent/admit':
                return {'allowed': False, 'until': 0, 'notice': True}
            return await api(method, path, payload)
        fri.api = cooked
        trigger = message('fri you up')
        try:
            await asyncio.wait_for(agent.handle(trigger), 5)
            assert log == []  # The cooldown notice goes out with no typing before it.
            assert 'recharge' in trigger.reply.await_args.args[0]
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_follow_up_types_only_for_an_action_and_never_before_quiet_or_a_closing(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        no_debounce(monkeypatch)
        log = []
        record_typing(channel, log)
        try:
            answering(fri, log, ChatResult(status='completed', reply='nothing much', allow_closing=True))
            await asyncio.wait_for(agent.handle(message('fri what is up')), 5)
            log.clear()
            closing = message('gotcha')  # In the follow-up window: the local closing shortcut.
            closing.add_reaction = AsyncMock()
            closing.reactions = []
            await asyncio.wait_for(agent.handle(closing), 5)
            assert log == []
            answering(fri, log, ChatResult(status='completed', response_kind='quiet'))
            await asyncio.wait_for(agent.handle(message('anyway')), 5)
            assert log == ['model']  # Decided quiet: no typing first.
            log.clear()
            async def act(text, context):
                log.append('model')
                return ChatResult(status='completed', action={'kind': next(iter(context['actions']))})
            fri.chat_once.side_effect = act
            async def run(*args, **kwargs):
                log.append('action')
                raise Stop
            monkeypatch.setattr('fri.fri_runner.run', run)
            with pytest.raises(Stop):
                await asyncio.wait_for(agent.handle(message('can you check the last few messages')), 5)
            assert log == ['model', 'typing', 'action', 'stopped']  # Words are coming once an action runs.
        finally:
            await fri.close()
    asyncio.run(scenario())


def history_of(channel, rows):
    async def history(*, limit=100, after=None, oldest_first=None, **kwargs):
        for item in sorted((m for m in rows.values() if m.id > after.id), key=lambda m: m.id)[:limit]:
            yield item
    channel.history = history


@pytest.mark.parametrize('later', [None, 'same author', 'bot', 'someone else', 'someone else after 20 bot posts',
                                   'unreadable', 'disconnected'])
def test_an_ambient_reply_is_dropped_once_someone_else_spoke(monkeypatch, tmp_path, later):
    async def scenario():
        fri, agent, store, _, channel, message, _, rows = environment(monkeypatch, tmp_path, channel_id=1)
        log = []
        record_typing(channel, log)
        history_of(channel, rows)
        picked = message('does anyone know if the server is down?')
        async def answer(text, context):
            log.append('model')
            if later == 'same author':
                message('nvm i think it is back')
            elif later == 'bot':
                message('[Gene] now playing', user=55).author.bot = True
            elif later == 'someone else':
                message('yeah it is down, give it a minute', user=21)
            elif later == 'someone else after 20 bot posts':
                for _ in range(20):
                    message('[Gene] now playing', user=55).author.bot = True
                message('yeah it is down, give it a minute', user=21)
            elif later in ('unreadable', 'disconnected'):
                channel.history = None  # Calling it fails like a refused read.
            return ChatResult(status='completed', reply='yeah, it went down a minute ago')
        fri.chat_once.side_effect = answer
        if later in ('unreadable', 'disconnected'):
            async def refused(*args, **kwargs):
                if later == 'disconnected':  # The connection dropped: no HTTP status at all.
                    raise aiohttp.ClientConnectionError('connection reset')
                raise discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'no')
                yield
            original = answer
            async def answer_then_refuse(text, context):
                result = await original(text, context)
                channel.history = refused
                return result
            fri.chat_once.side_effect = answer_then_refuse
        try:
            await asyncio.wait_for(agent.respond(picked, picked.content, ambient=True), 5)
            assert log == ['model']  # No typing for a turn nobody asked for.
            if later in ('someone else', 'someone else after 20 bot posts', 'unreadable', 'disconnected'):
                picked.reply.assert_not_awaited()  # Someone answered first, or it can't tell: stays quiet.
                assert not store.chat_context('123', 'public:1', '20')['turns']
            else:
                picked.reply.assert_awaited_once()
        finally:
            await fri.close()
    asyncio.run(scenario())


def rest(monkeypatch, seconds):
    monkeypatch.setattr('fri.fri_agent.REST_SECONDS', seconds)


@pytest.mark.parametrize('text', ['fri', 'yo fri', 'fri?', '<@99>', '<@99> hey'])
def test_a_bare_fri_waits_for_the_question_and_answers_both_once(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        rest(monkeypatch, 8)
        log = []
        record_typing(channel, log)
        answering(fri, log, ChatResult(status='completed', reply='under spawn. obviously'))
        bare = message(text)
        question = message('is the end portal under spawn or the other one')  # No "fri" in it.
        tasks = [asyncio.create_task(agent.handle(bare))]
        try:
            await asyncio.sleep(1.8)  # Past the usual 1.5 s debounce: today the bare line was answered here.
            fri.chat_once.assert_not_awaited()
            tasks.append(asyncio.create_task(agent.handle(question)))
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            fri.chat_once.assert_awaited_once()
            burst = [row['text'] for row in fri.chat_once.await_args.args[1].get('message_burst', [])]
            assert burst[-1] == question.content and len(burst) == 2  # One turn that sees both lines.
            question.reply.assert_awaited_once()
            bare.reply.assert_not_awaited()
            assert log == ['typing', 'model', 'stopped']  # It was spoken to Fri, so it types.
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_a_question_after_the_burst_span_is_answered_on_its_own(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='fri')
    async def scenario():
        fri, agent, _, _, channel, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        rest(monkeypatch, 8)
        log = []
        record_typing(channel, log)
        answering(fri, log, ChatResult(status='completed', reply='under spawn. obviously'))
        bare = message('<@99>')
        question = message('is the end portal under spawn')
        add = agent.bursts.add
        def arrive(event, arrived=None, *args, **kwargs):  # The question came 6 s after the ping.
            return add(event, arrived+6.1 if event is question else arrived, *args, **kwargs)
        agent.bursts.add = arrive
        tasks = [asyncio.create_task(agent.handle(bare))]
        try:
            await asyncio.sleep(0.1)
            tasks.append(asyncio.create_task(agent.handle(question)))
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            fri.chat_once.assert_awaited_once()
            context = fri.chat_once.await_args.args[1]
            assert context.get('message_burst', []) == []  # Its own turn...
            assert any(row['id'] == str(bare.id) for row in context['recent'])  # ...with the ping in view.
            question.reply.assert_awaited_once()
            bare.reply.assert_not_awaited()
            assert f'Mention 1/{bare.id} not answered: rest came separately' in caplog.messages
            assert log == ['typing', 'model', 'stopped']
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_a_question_sent_inside_the_wait_is_answered_when_its_checks_finish_after_it(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        rest(monkeypatch, 1)
        bare = message('<@99>')
        question = message('is the end portal under spawn')  # No "fri" in it, sent 0.5 s into the 1 s wait.
        seen = []
        async def answer(text, context):
            seen.append(context.get('message', text))
            if len(seen) == 1:
                await asyncio.sleep(1.5)  # Still writing the bare ping's reply when the question's checks end.
            return ChatResult(status='completed', reply='under spawn. obviously')
        fri.chat_once.side_effect = answer
        incoming = agent.incoming
        async def held(event, *args, **kwargs):  # The question's profile read is held past the deadline.
            if event is question:
                await asyncio.sleep(1.1)
            return await incoming(event, *args, **kwargs)
        agent.incoming = held
        tasks = [asyncio.create_task(agent.handle(bare))]
        try:
            await asyncio.sleep(0.5)
            tasks.append(asyncio.create_task(agent.handle(question)))
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
            question.reply.assert_awaited_once()  # Sent in time: answered, not dropped as unaddressed.
            assert fri.chat_once.await_count == 2
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            await fri.close()
    asyncio.run(scenario())


def test_a_bare_fri_with_nothing_after_it_is_answered_after_the_wait(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        rest(monkeypatch, 2.5)
        fri.chat_once.return_value = ChatResult(status='completed', reply='what')
        bare = message('fri')
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.wait_for(agent.handle(bare), 5)
            assert loop.time()-started >= 2.4
            bare.reply.assert_awaited_once()
            assert (1, 20) not in agent.summons
        finally:
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['fri what is the seed', 'fri forget me', 'friday?'])
def test_anything_more_than_a_bare_fri_keeps_the_short_debounce(monkeypatch, tmp_path, text):
    async def scenario():
        fri, agent, _, _, _, message, _, _ = environment(monkeypatch, tmp_path, channel_id=1)
        rest(monkeypatch, 8)
        fri.chat_once.return_value = ChatResult(status='completed', reply='no idea')
        try:
            await asyncio.wait_for(agent.handle(message(text)), 3)
            assert not agent.summons
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_a_bare_fri_replying_to_someone_or_with_an_image_does_not_wait():
    agent = Agent(SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, user=SimpleNamespace(id=99)))
    def msg(**extra):
        return SimpleNamespace(author=SimpleNamespace(bot=False), reference=None, attachments=[], stickers=[], **extra)
    assert agent.summoning(msg(), 'fri', None)
    assert not agent.summoning(msg(), 'fri', 'disable')
    assert not agent.summoning(msg(), None, None)
    assert not agent.summoning(SimpleNamespace(author=SimpleNamespace(bot=False), reference=SimpleNamespace(message_id=1),
                                               attachments=[], stickers=[]), 'hey', None)
    assert not agent.summoning(SimpleNamespace(author=SimpleNamespace(bot=False), reference=None,
                                               attachments=[object()], stickers=[]), 'hey', None)
