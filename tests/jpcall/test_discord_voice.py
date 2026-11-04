"""Real discord.py player/send path with synthetic codec, keys, socket and websocket."""

import asyncio
import inspect
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import davey
import discord
import pytest
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import ConnectionFlowState

from fri.jpcall.contract import CallRejected
from fri.jpcall.devbot import tone_pcm
from fri.jpcall.discord_audio import OUTPUT_FRAME, CallAudioSource
from fri.jpcall.discord_voice import CallVoiceClient, audio_ready, wait_audio_ready


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('network is forbidden in these tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


def voice_client():
    loop = asyncio.get_running_loop()
    own = SimpleNamespace(self_mute=False, mute=False, suppress=False, self_deaf=True)
    channel = SimpleNamespace(guild=SimpleNamespace(me=SimpleNamespace(voice=own)))
    client = SimpleNamespace(_connection=SimpleNamespace(loop=loop), loop=loop)
    voice = CallVoiceClient(client, channel)
    state = voice._connection
    state.state = ConnectionFlowState.connected
    state.dave_protocol_version = 1
    state.dave_session = SimpleNamespace(ready=True, status=davey.SessionStatus.active,
                                        encrypt_opus=MagicMock(side_effect=lambda data: b'ee' + data))
    state.mode = 'aead_xchacha20_poly1305_rtpsize'
    state.secret_key = [0] * 32  # Synthetic transport key; never used on a real connection.
    state.ssrc = 42
    state.socket = SimpleNamespace(sendall=MagicMock())
    state.ws = SimpleNamespace(speak=AsyncMock())
    voice.track_speaking()
    return voice


def test_installed_dave_transition_api_matches_installed_discord_gateway():
    # 0.2 requires explicit transition calls; 2.7.1 lacks them. This guard fails
    # for the incompatible pair even though discord.py's dependency bound permits it.
    gateway = inspect.getsource(DiscordVoiceWebSocket.received_binary_message)
    if hasattr(davey.DaveSession, 'prepare_transition'):
        assert 'prepare_transition(' in gateway, 'discord.py does not activate davey 0.2 MLS keys'


def test_waits_for_mls_ready_and_times_out_without_playback():
    async def run():
        voice = SimpleNamespace(is_connected=lambda: True, _connection=SimpleNamespace(
            dave_protocol_version=1, dave_session=SimpleNamespace(ready=False)))
        started = []

        async def start():
            await wait_audio_ready(voice, timeout=1)
            started.append(True)

        task = asyncio.create_task(start())
        await asyncio.sleep(0)
        assert not started and not audio_ready(voice)
        voice._connection.dave_session.ready = True
        await task
        assert started == [True]
        voice._connection.dave_session.ready = False
        with pytest.raises(CallRejected, match='voice encryption is not ready'):
            await wait_audio_ready(voice, timeout=0.001)
        voice._connection.dave_session.ready = True
        voice._connection.dave_session.can_encrypt = False
        assert not audio_ready(voice)
    asyncio.run(run())


def test_pending_dave_never_sends_plaintext_and_udp_errors_are_counted():
    async def run():
        voice = voice_client()
        try:
            state = voice._connection
            state.dave_session.ready = False
            voice.send_audio_packet(b'\xf8\xff\xfe', encode=False)
            state.socket.sendall.assert_not_called()
            state.dave_session.encrypt_opus.assert_not_called()
            assert voice.audio_stats(CallAudioSource())['packets_blocked'] == 1
            state.dave_session.ready = True
            state.socket.sendall.side_effect = OSError('synthetic socket failure')
            voice.send_audio_packet(b'\xf8\xff\xfe', encode=False)
            assert voice.audio_stats(CallAudioSource())['packets_failed'] == 1
        finally:
            voice._connection._socket_reader.stop()
    asyncio.run(run())


def test_normal_player_reads_five_seconds_and_sends_speaking_and_encrypted_packets(monkeypatch):
    class Encoder:
        SAMPLES_PER_FRAME = 960

        def __init__(self, **kwargs):
            self.frames = 0

        def encode(self, data, count):
            assert len(data) == OUTPUT_FRAME and count == 960
            self.frames += 1
            return b'\xf8\xff\xfe'  # Fake codec; framing, DAVE and UDP paths are real.

    class FiniteSource(CallAudioSource):
        def read(self):
            if self.stats()['frames_read'] == 250:
                self.close()
            return super().read()

    async def run():
        monkeypatch.setattr(discord.voice_client.opus, 'Encoder', Encoder)
        voice = voice_client()
        source = FiniteSource()
        source.push('fri', tone_pcm(5))
        assert not source.is_opus()
        finished = asyncio.get_running_loop().create_future()

        loop = asyncio.get_running_loop()
        voice.play(source, after=lambda error: loop.call_soon_threadsafe(finished.set_result, error))
        player = voice._player
        try:
            assert await asyncio.wait_for(finished, 7) is None
            await asyncio.to_thread(player.join, 1)
            stats = voice.audio_stats(source)
            assert stats['play_calls'] == 1
            assert stats['frames_read'] == stats['non_silent_frames'] == 250
            assert stats['eof_reads'] == 1 and source.closed
            assert stats['packets_sent'] == 255  # 250 frames plus five final Opus silence packets.
            assert stats['packets_failed'] == stats['packets_blocked'] == 0
            assert stats['speaking_on'] == stats['speaking_off'] == 1
            assert voice.encoder.frames == 250
            assert voice._connection.dave_session.encrypt_opus.call_count == 255
            assert stats['dave_ready'] and stats['audio_ready']
        finally:
            voice.stop()
            voice._connection._socket_reader.stop()
    asyncio.run(run())


def test_diagnostic_logs_counts_and_states_without_session_data(caplog):
    import json
    import logging

    async def run():
        voice = voice_client()
        source = CallAudioSource()
        try:
            voice._connection.token = 'synthetic-private-token'
            voice._connection.endpoint = 'synthetic-private-endpoint'
            voice._connection.dave_session.get_encryption_stats = lambda: SimpleNamespace(
                successes=4, failures=0, attempts=4)
            source.push('fri', tone_pcm(0.02))
            source.read()
            caplog.set_level(logging.INFO, logger='fri.jpcall.discord_voice')
            voice.log_audio(source, 'playing')
            line = caplog.records[-1].getMessage()
            assert 'synthetic-private' not in line
            data = json.loads(line.split('playing ', 1)[1])
            assert data['frames_read'] == data['non_silent_frames'] == 1
            assert data['dave_status'] == 'active' and data['dave_ready']
            assert data['dave_encrypt_successes'] == data['dave_encrypt_attempts'] == 4
            assert data['dave_encrypt_failures'] == 0
            assert all(value is None or type(value) in (str, bool, int) for value in data.values())
        finally:
            voice._connection._socket_reader.stop()
    asyncio.run(run())
