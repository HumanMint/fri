import asyncio
import socket
from types import SimpleNamespace

import pytest

from fri.jpcall import devbot
from fri.jpcall.contract import (
    CallFinished,
    CallRequest,
    Limits,
    QuestionAsked,
    QuestionClosed,
    Translation,
    UsageUpdated,
    Utterance,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network is forbidden in these tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


class Audio:
    def __init__(self):
        self.frames = []

    def push(self, speaker, pcm):
        self.frames.append((speaker, pcm))


def request():
    return CallRequest('r', 1, 2, 10, '+810000000000', 'dinner', ('party of two',), ('no fees',))


def test_fake_engine_script_audio_question_and_late_usage(monkeypatch):
    async def run():
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            await original_sleep(0)

        monkeypatch.setattr(devbot, 'asyncio', SimpleNamespace(
            get_running_loop=asyncio.get_running_loop, current_task=asyncio.current_task,
            Lock=asyncio.Lock,
            create_task=asyncio.create_task, sleep=fast_sleep, wait_for=asyncio.wait_for,
            shield=asyncio.shield, CancelledError=asyncio.CancelledError))
        engine, audio, events, answers = devbot.FakeEngine(), Audio(), [], []

        async def emit(event):
            events.append(event)
            if isinstance(event, QuestionAsked):
                session = engine.sessions[-1]
                answers.append(await session.answer(event.question_id, 'yes', 10))
                answers.append(await session.answer(event.question_id, 'no', 11))

        brief = await engine.plan(request())
        assert brief.share == ('party of two',) and brief.never == ('no fees',)
        session = await engine.start(brief, request(), audio, SimpleNamespace(emit=emit))
        await session.task
        assert answers == [True, False]
        assert len(audio.frames) == 75
        assert all(len(pcm) == 320 and any(pcm) for _, pcm in audio.frames)
        assert {speaker for speaker, _ in audio.frames} == {'fri', 'callee'}
        assert [e.item_id for e in events if isinstance(e, Utterance)] == ['1', '2', '3']
        assert [e.item_id for e in events if isinstance(e, Translation)] == ['1', '2', '3']
        finished = next(e for e in events if isinstance(e, CallFinished))
        assert not finished.usage_final and finished.usage[-1].usd is None
        assert isinstance(events[-1], UsageUpdated) and events[-1].usage_final
        assert events[-1].usage[-1].exact
        await engine.close()
    asyncio.run(run())


def test_fake_hangup_closes_question_and_finishes_once(monkeypatch):
    async def run():
        engine, events, question = devbot.FakeEngine(), [], asyncio.Event()

        async def speak(self, *args):
            return

        monkeypatch.setattr(devbot.FakeSession, 'speak', speak)

        async def emit(event):
            events.append(event)
            if isinstance(event, QuestionAsked):
                question.set()

        session = await engine.start(await engine.plan(request()), request(), Audio(),
                                     SimpleNamespace(emit=emit))
        await question.wait()
        await session.hang_up()
        assert sum(isinstance(e, CallFinished) for e in events) == 1
        assert any(isinstance(e, QuestionClosed) and e.reason == 'call_ended' for e in events)
        assert session.task.cancelled()
        await engine.close()
    asyncio.run(run())


def test_devbot_needs_no_gene_settings_and_uses_send_only_intents():
    async def run():
        bot = devbot.DevBot(devbot.FakeEngine(), 1, Limits())
        assert bot.intents.guilds and bot.intents.voice_states
        assert not bot.intents.message_content and not bot.intents.messages
        await bot.close()
    asyncio.run(run())


def test_main_reads_only_development_settings_and_requires_explicit_allowlist(monkeypatch):
    ran = []
    monkeypatch.setattr(devbot.os, 'environ', {'JPCALL_DEV_DISCORD_TOKEN': 'synthetic-token',
                                             'JPCALL_DEV_GUILD_ID': '1'})
    monkeypatch.setattr(devbot.DevBot, 'run', lambda self, token, **kwargs: ran.append(
        (token, self.guild_id, self.limits.allowed_user_ids)))
    monkeypatch.setattr('sys.argv', ['devbot', '--fake-engine', '--allow-user', '10'])
    devbot.main()
    assert ran == [('synthetic-token', 1, frozenset({10}))]
