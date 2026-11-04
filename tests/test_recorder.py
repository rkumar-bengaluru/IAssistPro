import asyncio

import numpy as np
import soundfile as sf

from iassistpro.recorder import AudioSink, encode


def write_stereo_sine(path, seconds):
    t = np.arange(int(16000 * seconds)) / 16000
    left = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    right = (0.3 * np.sin(2 * np.pi * 330 * t) * 32767).astype(np.int16)
    path.write_bytes(np.stack([left, right], axis=1).tobytes())
    return path


async def test_encode_writes_small_mono_ogg_off_the_loop(tmp_path):
    raw = write_stereo_sine(tmp_path / "audio.raw", seconds=3)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    info = await encode(raw, tmp_path / "audio.ogg", keep_wav=False)
    t.cancel()
    assert info.state == "ready" and info.format == "ogg_opus" and abs(info.duration_ms - 3000) <= 50
    assert sf.info(tmp_path / "audio.ogg").channels == 1 and not raw.exists() and ticks > 5
    assert info.bytes == (tmp_path / "audio.ogg").stat().st_size and info.bytes < 3 * 16000 * 2


async def test_encode_keep_wav_writes_debug_stereo(tmp_path):
    raw = write_stereo_sine(tmp_path / "audio.raw", seconds=1)
    info = await encode(raw, tmp_path / "audio.ogg", keep_wav=True)
    assert info.state == "ready" and sf.info(tmp_path / "audio.stereo.wav").channels == 2


async def test_encode_empty_raw_is_none(tmp_path):
    (tmp_path / "audio.raw").write_bytes(b"")
    assert (await encode(tmp_path / "audio.raw", tmp_path / "audio.ogg", keep_wav=False)).state == "none"
    assert (await encode(tmp_path / "missing.raw", tmp_path / "audio.ogg", keep_wav=False)).state == "none"


async def test_encode_failure_is_failed(tmp_path):
    raw = write_stereo_sine(tmp_path / "audio.raw", seconds=1)
    info = await encode(raw, tmp_path / "no_such_dir" / "audio.ogg", keep_wav=False)
    assert info.state == "failed"


async def test_sink_appends_chunks(tmp_path):
    sink = AudioSink(tmp_path / "audio.raw")
    await sink.on_audio_data(None, b"\x01\x00\x02\x00", 16000, 2)
    await sink.on_audio_data(None, b"\x03\x00\x04\x00", 16000, 2)
    sink.close()
    assert (tmp_path / "audio.raw").read_bytes() == b"\x01\x00\x02\x00\x03\x00\x04\x00" and sink.bytes == 8
