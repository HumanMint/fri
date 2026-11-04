import audioop
import struct
from concurrent.futures import ThreadPoolExecutor

from fri.jpcall.discord_audio import BUFFER_BYTES, OUTPUT_FRAME, CallAudioSource


def pcm(value, count=160):
    return struct.pack('<h', value) * count


def samples(frame):
    return struct.unpack('<' + 'h' * (len(frame) // 2), frame)


def test_frames_silence_and_close():
    source = CallAudioSource()
    for _ in range(3):
        assert source.read() == bytes(OUTPUT_FRAME)
    source.push('callee', pcm(1000, 80))
    frame = source.read()
    assert len(frame) == OUTPUT_FRAME
    assert max(samples(frame)) == 1000
    assert samples(frame)[::2] == samples(frame)[1::2]
    source.close()
    source.push('fri', pcm(1000))
    assert source.read() == b''


def test_mixing_clipping_and_clear():
    for first, second, expected in [(1000, 2000, 3000), (30000, 30000, 32767),
                                    (-30000, -30000, -32768)]:
        source = CallAudioSource()
        source.push('callee', pcm(first, 320))
        source.push('fri', pcm(second, 320))
        assert samples(source.read())[-1] == expected
        source.clear('fri')
        assert samples(source.read())[-1] == first
        source.clear('callee')
        assert source.read() == bytes(OUTPUT_FRAME)


def test_drop_oldest_and_resampler_continuity():
    source = CallAudioSource()
    source.push('callee', pcm(1000, 1000) + pcm(2000, BUFFER_BYTES // 2))
    assert len(source._buffers['callee']) == BUFFER_BYTES
    assert samples(source.read())[-1] == 2000
    source = CallAudioSource()
    wave = struct.pack('<320h', *range(-160, 160))
    source.push('fri', wave)
    first = source.read()
    second = source.read()
    expected, _ = audioop.ratecv(wave, 2, 1, 8000, 48000, None)
    assert first + second == audioop.tostereo(bytes(10) + expected, 2, 1, 1)


def test_threaded_push_read_clear_close():
    source = CallAudioSource()

    def writer():
        for i in range(2000):
            source.push('fri', pcm(i))
            source.push('callee', pcm(-i))
            if i % 7 == 0:
                source.clear('fri')
        source.close()

    def reader():
        for _ in range(2000):
            assert len(source.read()) in (0, OUTPUT_FRAME)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(writer), pool.submit(reader)]
        for future in futures:
            future.result(timeout=5)
    assert source.read() == b''


def test_empty_buffer_returns_silence_even_after_nonzero_last_sample():
    source = CallAudioSource()
    source.push('fri', pcm(1000))
    assert any(source.read())
    assert source.read() == bytes(OUTPUT_FRAME)
