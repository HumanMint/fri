"""Voice readiness and payload-free diagnostics around discord.py's normal player."""

import asyncio
import json
import logging
import threading

import davey
import discord
from discord.voice_state import VoiceConnectionState

from .contract import CallRejected

LOG = logging.getLogger(__name__)


def audio_ready(voice):
    if not voice.is_connected():
        return False
    state = voice._connection
    if state.dave_protocol_version == 0:
        return True
    session = state.dave_session
    return (session is not None and session.ready
            and getattr(session, 'can_encrypt', True))


async def wait_audio_ready(voice, *, timeout=10.0, diagnostic=None):
    previous = None
    try:
        async with asyncio.timeout(timeout):
            while not audio_ready(voice):
                if diagnostic is not None:
                    state = voice.dave_state()
                    if state != previous:
                        voice.log_audio(diagnostic, 'waiting')
                        previous = state
                await asyncio.sleep(0.05)
            if diagnostic is not None:
                voice.log_audio(diagnostic, 'ready')
    except TimeoutError:
        if diagnostic is not None:
            voice.log_audio(diagnostic, 'not_ready')
        raise CallRejected('voice encryption is not ready. try again.') from None


class _CallConnection(VoiceConnectionState):
    def send_packet(self, packet):
        # VoiceClient.send_audio_packet swallows OSError, so count at the UDP boundary.
        try:
            super().send_packet(packet)
        except OSError:
            self.voice_client.count('packets_failed')
            raise
        else:
            self.voice_client.count('packets_sent')


class CallVoiceClient(discord.VoiceClient):
    def __init__(self, client, channel):
        self._counts_lock = threading.Lock()
        self._counts = dict(play_calls=0, packets_sent=0, packets_failed=0, packets_blocked=0,
                            speaking_on=0, speaking_off=0, speaking_failed=0)
        self._tracked_ws = None
        super().__init__(client, channel)

    def create_connection_state(self):
        return _CallConnection(self)

    def count(self, key):
        with self._counts_lock:
            self._counts[key] += 1

    def track_speaking(self):
        ws = self.ws
        if ws is self._tracked_ws:
            return
        self._tracked_ws = ws
        speak = ws.speak

        async def counted_speak(state=discord.SpeakingState.voice):
            try:
                await speak(state)
            except Exception:
                self.count('speaking_failed')
                raise
            else:
                self.count('speaking_on' if int(state) else 'speaking_off')

        ws.speak = counted_speak

    def play(self, source, **kwargs):
        super().play(source, **kwargs)
        self.count('play_calls')

    def send_audio_packet(self, data, *, encode=True):
        # Do not send plaintext audio while negotiated DAVE is waiting for MLS keys.
        # Initial playback waits before starting the player, retaining queued audio.
        if not audio_ready(self):
            self.count('packets_blocked')
            return
        super().send_audio_packet(data, encode=encode)

    def dave_state(self):
        state = self._connection
        session = state.dave_session
        status = 'absent'
        if session is not None:
            status = 'unknown'
            for name in ('inactive', 'pending', 'awaiting_response', 'active'):
                if session.status == getattr(davey.SessionStatus, name):
                    status = name
                    break
        return dict(dave_protocol=state.dave_protocol_version, dave_status=status,
                    dave_ready=bool(session is not None and session.ready),
                    audio_ready=bool(audio_ready(self)), connected=self.is_connected())

    def audio_stats(self, source):
        with self._counts_lock:
            result = dict(self._counts)
        result.update(source.stats())
        result.update(self.dave_state())
        result.update(player_running=self.is_playing(), player_paused=self.is_paused())
        session = self._connection.dave_session
        get_stats = getattr(session, 'get_encryption_stats', None)
        encryption = get_stats() if get_stats is not None else None
        for name in ('successes', 'failures', 'attempts'):
            result[f'dave_encrypt_{name}'] = getattr(encryption, name, None)
        own = self._connection.self_voice_state
        for name in ('self_mute', 'mute', 'suppress', 'self_deaf'):
            result[name] = bool(getattr(own, name, False)) if own is not None else None
        return result

    def log_audio(self, source, phase):
        LOG.info('jpcall audio check %s %s', phase,
                 json.dumps(self.audio_stats(source), sort_keys=True))

    async def monitor_audio(self, source):
        try:
            while True:
                self.track_speaking()
                self.log_audio(source, 'playing')
                await asyncio.sleep(1)
        finally:
            self.log_audio(source, 'finished')
