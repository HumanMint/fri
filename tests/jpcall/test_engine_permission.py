"""Permission response time starts after the bounded notice has played."""

import asyncio

from test_engine_flow import SID, PhoneMedia, dial, make_engine, settled
from test_engine_transport import EVENTS


class DelayedPhone(PhoneMedia):
    def __init__(self, call):
        super().__init__(call)
        self.marks = []

    async def mark(self, name):
        self.marks.append(name)

    async def finish_playback(self):
        for mark in self.marks:
            await self.call.mark(mark)
        self.marks.clear()
        for _ in range(20):
            await asyncio.sleep(0)


async def notice(call):
    await call.status({'CallSid': SID, 'CallStatus': 'in-progress', 'SequenceNumber': '1'})
    media = DelayedPhone(call)
    await call.start(media)
    await call.status({'CallSid': SID, 'AnsweredBy': 'human'})
    await call._rt.handle_event({'type': EVENTS['speech_started'], 'item_id': 'hello', 'audio_start_ms': 0})
    await call._rt.handle_event({'type': EVENTS['speech_stopped'], 'item_id': 'hello'})
    await settled(lambda: bool(media.marks) and call._rt._conversation_open)
    return media


async def turn_clock(clock, seconds):
    clock.advance(seconds)
    for _ in range(30):
        await asyncio.sleep(0)


def test_permission_window_starts_after_notice_phone_playback(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch, call_language='ko')
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)
            await turn_clock(clock, 18)
            await media.finish_playback()
            await turn_clock(clock, 2)
            assert not call._ending  # The answer-time deadline must not consume the response window.
            await turn_clock(clock, 17)
            assert not call._ending
            await turn_clock(clock, 1)
            await settled(lambda: call._ending)
            assert call._detail == 'Permission to share the call was not established.'
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_notice_playback_itself_has_a_bounded_deadline(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            await notice(call)
            await turn_clock(clock, 29)
            assert not call._ending
            await turn_clock(clock, 1)
            await settled(lambda: call._ending)
            assert call._detail == 'The call notice could not be delivered.'
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_transfer_never_inherits_an_old_permission_deadline(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            media = await notice(call)
            await media.finish_playback()
            await call._handle_tool('permission', {'decision': 'granted'}, 'initial-consent')
            await settled(lambda: call._rt._idle.is_set())
            await media.finish_playback()
            await turn_clock(clock, 10)
            previous = call._hold
            await call._tool('permission', {'decision': 'transfer'}, 'new-person')
            await settled(lambda: call._hold is not previous and call._rt._conversation_open and bool(media.marks))
            await turn_clock(clock, 10)
            assert not call._ending
            await media.finish_playback()
            await turn_clock(clock, 19)
            assert not call._ending
            await turn_clock(clock, 1)
            await settled(lambda: call._ending)
            assert call._detail == 'Permission to share the call was not established.'
            await call.hang_up()
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())


def test_stale_notice_drain_timeout_cannot_end_a_new_permission_phase(tmp_path, monkeypatch):
    async def run():
        engine, provider, peer, http, clock, runner = await make_engine(tmp_path, monkeypatch)
        try:
            call, brief, audio, events = await dial(engine, provider)
            entered, release = asyncio.Event(), asyncio.Event()

            async def drain(*, timeout_s):
                entered.set()
                await release.wait()
                raise TimeoutError

            monkeypatch.setattr(call._rt, 'drain', drain)
            task = call._spawn(call._notice_delivered(call.consent_generation))
            await entered.wait()
            call._permission(True)
            call._permission(False)
            release.set()
            await task
            await asyncio.sleep(0)
            assert not call._ending
            assert call._notice_delivered_generation is None
        finally:
            await engine.close()
            await http.aclose()
    asyncio.run(run())
