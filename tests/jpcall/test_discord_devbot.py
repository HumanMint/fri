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


def test_audio_check_tone_is_five_seconds_at_440_hz_and_minus_twelve_dbfs():
    import math
    import struct

    pcm = devbot.tone_pcm(5)
    assert len(pcm) == 8000 * 5 * 2
    samples = struct.unpack('<40000h', pcm)
    peak = max(abs(sample) for sample in samples)
    assert abs(20 * math.log10(peak / 32767) + 12) < 0.01
    assert sum(first <= 0 < second for first, second in zip(samples, samples[1:])) == 2200


def test_audio_check_mode_plays_only_synthetic_audio_and_zero_cost(monkeypatch):
    async def run():
        slept = []

        async def sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(devbot, 'asyncio', SimpleNamespace(
            get_running_loop=asyncio.get_running_loop, current_task=asyncio.current_task,
            Lock=asyncio.Lock, create_task=asyncio.create_task, sleep=sleep,
            CancelledError=asyncio.CancelledError))
        engine, audio, events = devbot.FakeEngine(audio_check=True), Audio(), []

        async def emit(event):
            events.append(event)

        brief = await engine.plan(request())
        assert 'five seconds' in brief.summary_en
        session = await engine.start(brief, request(), audio, SimpleNamespace(emit=emit))
        await session.task
        assert slept == [5.25]
        assert len(audio.frames) == 1 and len(audio.frames[0][1]) == 80000
        assert len(events) == 1 and isinstance(events[0], CallFinished)
        assert events[0].duration_s == 5 and events[0].usage_final
        assert all(line.usd == 0 for line in events[0].usage)
        await engine.close()
    asyncio.run(run())


def test_audio_check_cli_requires_fake_engine_and_sets_both_modes(monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys, 'argv', ['devbot', '--audio-check'])
    with pytest.raises(SystemExit):
        devbot.main()
    assert '--audio-check requires --fake-engine' in capsys.readouterr().err
    bots = []
    monkeypatch.setattr(devbot.os, 'environ', {'JPCALL_DEV_DISCORD_TOKEN': 'synthetic-token',
                                             'JPCALL_DEV_GUILD_ID': '1'})
    monkeypatch.setattr(devbot.DevBot, 'run', lambda self, *a, **k: bots.append(self))
    monkeypatch.setattr(sys, 'argv', ['devbot', '--fake-engine', '--audio-check',
                                    '--allow-user', '10'])
    devbot.main()
    assert bots[0].audio_check and bots[0].engine.audio_check
    assert bots[0].limits.allowed_user_ids == frozenset({10})
