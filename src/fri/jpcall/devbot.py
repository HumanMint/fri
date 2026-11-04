"""Standalone Discord runner and a fully local scripted call engine.

Fake mode uses --fake-engine and explicit --allow-user IDs. Real mode reads the
engine configuration from process env. Importing needs no environment or network.
"""

import argparse
import asyncio
import logging
import math
import os
import struct

import discord
from discord import app_commands

from .contract import (
    CallBrief,
    CallFinished,
    CallRejected,
    CallState,
    Limits,
    QuestionAsked,
    QuestionClosed,
    StatusChanged,
    Translation,
    UsageLine,
    UsageUpdated,
    Utterance,
)
from .discord_command import NO_MENTIONS, register
from .discord_runtime import disabled_message, load_config

CALL_LINES = {
    'ja': ('AI通訳です。お客様から質問があるのですが、よろしいでしょうか。',
           '午後六時なら空いています。', 'ありがとうございます。'),
    'ko': ('저는 손님 대신 통역을 도와드리는 AI입니다. 질문이 있는데 괜찮으실까요?',
           '오후 여섯 시에 가능합니다.', '감사합니다.'),
}

TONE_PEAK = round(32767 * 10 ** (-12 / 20))


def tone_pcm(seconds, frequency=440):
    return struct.pack(f'<{round(8000 * seconds)}h', *(
        round(TONE_PEAK * math.sin(2 * math.pi * frequency * sample / 8000))
        for sample in range(round(8000 * seconds))))


class FakeSession:
    def __init__(self, request, audio, events, *, originals=CALL_LINES['ja']):
        self.request, self.audio, self.events = request, audio, events
        self.originals = originals
        self.answer_future = asyncio.get_running_loop().create_future()
        self.question_open = False
        self.ended = False
        self.task = None
        self._finish_lock = asyncio.Lock()

    async def emit(self, event_type, *args):
        await self.events.emit(event_type(self.request.request_id, *args))

    async def speak(self, item_id, speaker, original, english, frequency):
        await self.emit(Utterance, item_id, speaker, original, 0.0)
        # Keep tone phase continuous across frames. Synthetic audio only.
        for frame in range(25):
            pcm = struct.pack('<160h', *(int(TONE_PEAK * math.sin(2 * math.pi * frequency *
                                              (frame * 160 + sample) / 8000))
                                        for sample in range(160)))
            self.audio.push(speaker, pcm)
            await asyncio.sleep(0.02)
        await self.emit(Translation, item_id, english)

    async def answer(self, question_id, answer, answered_by):
        if (question_id != 'time' or not self.question_open or self.ended
                or self.answer_future.done() or not answer.strip()):
            return False
        self.question_open = False
        self.answer_future.set_result(answer.strip())
        await self.emit(QuestionClosed, 'time', 'answered', answer.strip())
        return True

    async def hang_up(self, reason='requested'):
        if self.task is not None and self.task is not asyncio.current_task():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.finish('fake call ended.')

    @staticmethod
    def usage(phone_posted=False):
        return (
            UsageLine('realtime', 'fake-voice', {'audio_in_tokens': 120, 'audio_out_tokens': 200,
                                               'text_in_tokens': 80, 'text_out_tokens': 60}, 0.02),
            UsageLine('transcription', 'fake-transcription', {'audio_in_tokens': 120}, 0.001),
            UsageLine('translation', 'fake-text', {'input_tokens': 180, 'output_tokens': 100}, 0.002),
            UsageLine('summary', 'fake-text', {'input_tokens': 240, 'output_tokens': 80}, 0.001),
            UsageLine('twilio', 'fake-phone', {'minutes': 0.25},
                      0.01 if phone_posted else None, exact=phone_posted),
        )

    async def finish(self, outcome, *, duration_s=15.0, usage_final=False):
        async with self._finish_lock:
            if self.ended:
                return
            self.ended = True
            if self.question_open:
                self.question_open = False
                await self.emit(QuestionClosed, 'time', 'call_ended')
            await self.emit(CallFinished, outcome, duration_s, self.usage(), usage_final)

    async def run(self):
        try:
            await self.emit(StatusChanged, CallState.DIALING, 'fake call. no phone line.')
            await self.emit(StatusChanged, CallState.IN_PROGRESS)
            await self.speak('1', 'fri', self.originals[0],
                             'i am an ai interpreter calling for the group. may i ask a question?',
                             440)
            await self.speak('2', 'callee', self.originals[1], 'six pm is available.', 660)
            self.question_open = True
            await self.emit(StatusChanged, CallState.ON_HOLD)
            await self.emit(QuestionAsked, 'time', 'does six pm work?', ('yes, six is fine',
                                                                       'ask about seven'), 30.0)
            try:
                answer = await asyncio.wait_for(asyncio.shield(self.answer_future), 30)
            except TimeoutError:
                self.question_open = False
                await self.emit(QuestionClosed, 'time', 'expired')
                answer = 'no answer from the group'
            await self.emit(StatusChanged, CallState.IN_PROGRESS)
            await self.speak('3', 'fri', self.originals[2], 'thank you.', 440)
            await self.finish(f'fake outcome · {answer}. no booking was made.')
            await asyncio.sleep(1)
            await self.emit(UsageUpdated, self.usage(phone_posted=True), True)
        except asyncio.CancelledError:
            raise


class AudioCheckSession(FakeSession):
    @staticmethod
    def usage(phone_posted=False):
        return (UsageLine('audio check', 'synthetic-tone', {'seconds': 5}, 0, True),)

    async def finish(self, outcome):
        await super().finish(outcome, duration_s=5.0, usage_final=True)

    async def run(self):
        self.audio.push('fri', tone_pcm(5))
        # Playback is already gated on DAVE readiness. Allow the player its 250
        # real-time frames plus a small scheduling margin before closing the source.
        await asyncio.sleep(5.25)
        await self.finish('audio check complete. counts are in the console.')


class FakeEngine:
    """Implements only CallEngine's contract; all prices and speech are synthetic."""

    def __init__(self, *, audio_check=False, call_language='ja'):
        self.sessions = []
        self.audio_check = audio_check
        self.originals = CALL_LINES[call_language]

    async def open(self):
        pass

    async def plan(self, request):
        if not request.goal.strip():
            raise CallRejected('give me a goal for the call.')
        summary = ('audio check · five seconds of 440 hz tone. no phone call.'
                   if self.audio_check else f'fake call · {request.goal}')
        return CallBrief(request.request_id, summary,
                         self.originals[0],
                         request.share, request.never)

    async def start(self, brief, request, audio, events):
        session_type = AudioCheckSession if self.audio_check else FakeSession
        session = session_type(request, audio, events, originals=self.originals)
        self.sessions.append(session)
        session.task = asyncio.create_task(session.run())
        return session

    async def close(self):
        for session in self.sessions:
            await session.hang_up('shutdown')


class DevBot(discord.Client):
    def __init__(self, engine, guild_id, limits, *, config=None, audio_check=False):
        intents = discord.Intents.none()
        intents.guilds = intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.engine, self.guild_id, self.limits = engine, guild_id, limits
        self.config = config
        self.audio_check = audio_check
        self.tree = app_commands.CommandTree(self)
        self.calls = None

    async def setup_hook(self):
        if self.engine is None:
            from .engine import JpcallEngine
            self.engine = JpcallEngine(self.config)
        try:
            await self.engine.open()
            self.calls = register(self.tree, self, self.engine, [self.guild_id], limits=self.limits,
                                  audio_check=self.audio_check)
            await self.tree.sync(guild=discord.Object(id=self.guild_id))
        except BaseException:
            await self.engine.close()
            raise

    async def close(self):
        try:
            if self.calls is not None:
                await self.calls.close()
        finally:
            try:
                if self.engine is not None:
                    await self.engine.close()
            finally:
                await super().close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fake-engine', action='store_true')
    parser.add_argument('--audio-check', action='store_true',
                        help='fake mode only: /jpcall plays a five-second tone and logs audio counts')
    parser.add_argument('--allow-user', type=int, action='append', default=[],
                        help='fake mode only: member allowed to start calls; repeat for more members')
    args = parser.parse_args()
    if args.audio_check and not args.fake_engine:
        parser.error('--audio-check requires --fake-engine.')
    if not args.fake_engine and args.allow_user:
        parser.error('--allow-user is for fake mode. real mode uses JPCALL_ALLOWED_USER_IDS.')
    token = os.environ.get('JPCALL_DEV_DISCORD_TOKEN')
    guild = os.environ.get('JPCALL_DEV_GUILD_ID', '')
    if not token or not guild.isdecimal() or int(guild) <= 0:
        parser.error('set JPCALL_DEV_DISCORD_TOKEN and JPCALL_DEV_GUILD_ID')
    config = None
    if args.fake_engine:
        language = os.environ.get('JPCALL_CALL_LANGUAGE', 'ja').strip().lower()
        if language not in CALL_LINES:
            parser.error('JPCALL_CALL_LANGUAGE must be ja or ko.')
        engine = FakeEngine(audio_check=args.audio_check, call_language=language)
        limits = Limits(allowed_user_ids=frozenset(args.allow_user))
    else:
        try:
            config = load_config()
        except ModuleNotFoundError as exc:
            if exc.name != 'fri.jpcall.config':
                raise
            parser.error('jpcall engine configuration is not installed.')
        if not config.enabled:
            parser.error(disabled_message(config))
        engine, limits = None, config.limits
    logging.basicConfig(level=logging.INFO)
    bot = DevBot(engine, int(guild), limits, config=config, audio_check=args.audio_check)
    bot.run(token, log_handler=None)


if __name__ == '__main__':
    main()
