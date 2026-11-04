"""Recording: stereo PCM16 16 kHz streamed to disk during the session, mono Ogg/Opus encoded after it (AS5)."""

import asyncio
from pathlib import Path

import numpy as np

from .contract import AudioInfo
from .logs import log_event

SAMPLE_RATE = 16000
BUFFER_SIZE = 1 << 20


class AudioSink:
    """Appends each AudioBufferProcessor on_audio_data chunk (interleaved stereo PCM16) to audio.raw."""

    def __init__(self, raw_path: Path):
        self.raw_path = Path(raw_path)
        self._f = open(self.raw_path, "ab")
        self.bytes = 0

    async def on_audio_data(self, _processor, audio: bytes, sample_rate: int, num_channels: int) -> None:
        if self._f.closed or not audio:
            return
        self._f.write(audio)
        self._f.flush()
        self.bytes += len(audio)

    def attach(self, buffer) -> None:
        buffer.add_event_handler("on_audio_data", self.on_audio_data)

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


def make_buffer():
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

    return AudioBufferProcessor(sample_rate=SAMPLE_RATE, num_channels=2, buffer_size=BUFFER_SIZE)


def _encode_sync(raw_path: Path, out_path: Path, keep_wav: bool) -> AudioInfo:
    import soundfile as sf

    data = np.frombuffer(raw_path.read_bytes(), dtype=np.int16)
    if data.size < 2:
        return AudioInfo(state="none")
    stereo = data[: data.size - data.size % 2].reshape(-1, 2)
    if keep_wav:
        sf.write(out_path.with_name("audio.stereo.wav"), stereo, SAMPLE_RATE, subtype="PCM_16")
    mono = stereo.astype(np.float32).mean(axis=1) / 32768.0
    sf.write(out_path, mono, SAMPLE_RATE, format="OGG", subtype="OPUS")
    if not keep_wav:
        raw_path.unlink(missing_ok=True)
    return AudioInfo(state="ready", format="ogg_opus", duration_ms=int(round(1000 * len(mono) / SAMPLE_RATE)),
                     bytes=out_path.stat().st_size)


async def encode(raw_path: Path, out_path: Path, keep_wav: bool) -> AudioInfo:
    raw_path, out_path = Path(raw_path), Path(out_path)
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        raw_path.unlink(missing_ok=True)
        return AudioInfo(state="none")
    try:
        info = await asyncio.to_thread(_encode_sync, raw_path, out_path, keep_wav)
    except Exception as e:
        log_event("audio_encode_failed", error=type(e).__name__)
        return AudioInfo(state="failed")
    log_event("audio_encoded", ms=info.duration_ms, bytes=info.bytes)
    return info
