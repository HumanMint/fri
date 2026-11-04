"""Send-only PCM source. The engine writes; discord.py's player thread reads."""

import audioop
import threading

import discord

from .contract import AUDIO_RATE, Speaker

INPUT_FRAME = 160 * 2
OUTPUT_FRAME = 960 * 2 * 2
BUFFER_LIMITS = {'callee': AUDIO_RATE * 2 * 2, 'fri': AUDIO_RATE * 2 * 60}


class CallAudioSource(discord.AudioSource):
    def __init__(self):
        self._lock = threading.Lock()
        self._buffers = {speaker: bytearray() for speaker in ('callee', 'fri')}
        self._state = None
        self._closed = False

    def push(self, speaker: Speaker, pcm16: bytes) -> None:
        if len(pcm16) % 2:
            raise ValueError('audio must contain whole PCM16 samples')
        # Slice before extending, so even a large incoming chunk has bounded storage.
        with self._lock:
            if self._closed:
                return
            buffer = self._buffers[speaker]
            limit = BUFFER_LIMITS[speaker]
            buffer.extend(pcm16[-limit:])
            del buffer[:max(0, len(buffer) - limit)]

    def clear(self, speaker: Speaker) -> None:
        with self._lock:
            self._buffers[speaker].clear()
            # ratecv retains a previous sample; don't leak a cleared speaker's tail.
            self._state = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for buffer in self._buffers.values():
                buffer.clear()
            self._state = None

    def cleanup(self) -> None:
        self.close()

    def read(self) -> bytes:
        with self._lock:
            if self._closed:
                return b''
            has_audio = any(self._buffers.values())
            frames = []
            for buffer in self._buffers.values():
                frame = bytes(buffer[:INPUT_FRAME])
                del buffer[:INPUT_FRAME]
                frames.append(frame.ljust(INPUT_FRAME, b'\0'))
            mixed = audioop.add(*frames, 2)  # Saturates, rather than wrapping PCM16.
            mono, self._state = audioop.ratecv(mixed, 2, 1, AUDIO_RATE, 48000, self._state)
            # The first interpolation needs five leading 48 kHz samples. Subsequent
            # reads produce exactly 960 samples, carrying ratecv's state forward.
            mono = mono.rjust(OUTPUT_FRAME // 2, b'\0')
            if not has_audio:
                return bytes(OUTPUT_FRAME)
            return audioop.tostereo(mono, 2, 1, 1)
