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


class FakeSession:
    def __init__(self, request, audio, events):
        self.request, self.audio, self.events = request, audio, events
        self.answer_future = asyncio.get_running_loop().create_future()
        self.question_open = False
        self.ended = False
        self.task = None
        self._finish_lock = asyncio.Lock()

    async def emit(self, event_type, *args):
        await self.events.emit(event_type(self.request.request_id, *args))

    async def speak(self, item_id, speaker, ja, en, frequency):
        await self.emit(Utterance, item_id, speaker, ja, 0.0)
        # Keep tone phase continuous across frames. Synthetic audio only.
        for frame in range(25):
            pcm = struct.pack('<160h', *(int(2000 * math.sin(2 * math.pi * frequency *
                                              (frame * 160 + sample) / 8000))
                                        for sample in range(160)))
            self.audio.push(speaker, pcm)
            await asyncio.sleep(0.02)
        await self.emit(Translation, item_id, en)

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

    async def finish(self, outcome):
        async with self._finish_lock:
            if self.ended:
                return
            self.ended = True
            if self.question_open:
                self.question_open = False
                await self.emit(QuestionClosed, 'time', 'call_ended')
            await self.emit(CallFinished, outcome, 15.0, self.usage(), False)

    async def run(self):
        try:
            await self.emit(StatusChanged, CallState.DIALING, 'fake call. no phone line.')
            await self.emit(StatusChanged, CallState.IN_PROGRESS)
            await self.speak('1', 'fri', 'AIアシスタントです。グループが聞いており、通話は文字起こしされます。',
                             'i am an ai assistant. the group is listening and the call is transcribed.',
                             440)
            await self.speak('2', 'callee', '午後六時なら空いています。', 'six pm is available.', 660)
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
            await self.speak('3', 'fri', 'ありがとうございます。', 'thank you.', 440)
            await self.finish(f'fake outcome · {answer}. no booking was made.')
            await asyncio.sleep(1)
            await self.emit(UsageUpdated, self.usage(phone_posted=True), True)
        except asyncio.CancelledError:
            raise


class FakeEngine:
    """Implements only CallEngine's contract; all prices and speech are synthetic."""

    def __init__(self):
        self.sessions = []

    async def open(self):
        pass

    async def plan(self, request):
        if not request.goal.strip():
            raise CallRejected('give me a goal for the call.')
        return CallBrief(request.request_id, f'fake call · {request.goal}',
                         'AIアシスタントです。グループが聞いており、通話は文字起こしされます。',
                         request.share, request.never)

    async def start(self, brief, request, audio, events):
        session = FakeSession(request, audio, events)
        self.sessions.append(session)
        session.task = asyncio.create_task(session.run())
        return session

    async def close(self):
        for session in self.sessions:
            await session.hang_up('shutdown')


class DevBot(discord.Client):
    def __init__(self, engine, guild_id, limits, *, config=None):
        intents = discord.Intents.none()
        intents.guilds = intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.engine, self.guild_id, self.limits = engine, guild_id, limits
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.calls = None

    async def setup_hook(self):
        if self.engine is None:
            from .engine import JpcallEngine
            self.engine = JpcallEngine(self.config)
        try:
            await self.engine.open()
            self.calls = register(self.tree, self, self.engine, [self.guild_id], limits=self.limits)
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
    parser.add_argument('--allow-user', type=int, action='append', default=[],
                        help='fake mode only: member allowed to start calls; repeat for more members')
    args = parser.parse_args()
    if not args.fake_engine and args.allow_user:
        parser.error('--allow-user is for fake mode. real mode uses JPCALL_ALLOWED_USER_IDS.')
    token = os.environ.get('JPCALL_DEV_DISCORD_TOKEN')
    guild = os.environ.get('JPCALL_DEV_GUILD_ID', '')
    if not token or not guild.isdecimal() or int(guild) <= 0:
        parser.error('set JPCALL_DEV_DISCORD_TOKEN and JPCALL_DEV_GUILD_ID')
    config = None
    if args.fake_engine:
        engine = FakeEngine()
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
    bot = DevBot(engine, int(guild), limits, config=config)
    bot.run(token, log_handler=None)


if __name__ == '__main__':
    main()
