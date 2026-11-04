"""Inbound mic level and silent-input detection (spec §3.1 step 2, §3.3; UX3)."""

import asyncio
import math
import random
import struct

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.workers.runner import WorkerRunner

from iassistpro.levels import InputLevelMonitor, LevelMeter

SR = 16000
N = 160  # 10 ms at 16 kHz


def _pack(samples) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def sine_frames(seconds: float, amp: float, channels: int = 1, start: int = 0) -> list[bytes]:
    out = []
    for f in range(round(seconds * 100)):
        s = []
        for i in range(N):
            v = int(round(amp * 32767 * math.sin(2 * math.pi * 440 * (start + f * N + i) / SR)))
            s.extend([v] * channels)
        out.append(_pack(s))
    return out


def zero_frames(seconds: float) -> list[bytes]:
    return [bytes(2 * N)] * round(seconds * 100)


def noise_frames(seconds: float, seed: int = 7) -> list[bytes]:
    rng = random.Random(seed)
    out = []
    for _ in range(round(seconds * 100)):
        s = [rng.choice((-2, -1, 0, 1, 2)) for _ in range(N)]
        if not any(s):
            s[0] = 1
        out.append(_pack(s))
    return out


def feed_all(m: LevelMeter, frames: list[bytes], t0: float = 0.0, channels: int = 1) -> list[dict]:
    msgs = []
    for i, f in enumerate(frames):
        msgs.extend(m.feed(f, SR, channels, t0 + i * 0.01))
    return msgs


def silent(msgs):
    return [x for x in msgs if x["type"] == "silent_input"]


def test_level_every_200_ms_with_rms_in_full_scale_units():
    m = LevelMeter(); m.start(0)
    msgs = feed_all(m, sine_frames(1.0, amp=0.1))
    levels = [x for x in msgs if x["type"] == "mic_level"]
    assert len(levels) == 5 and all(abs(x["rms"] - 0.0707) < 0.002 for x in levels)


def test_digital_silence_has_rms_zero():
    m = LevelMeter(); m.start(0)
    levels = [x for x in feed_all(m, zero_frames(1.0)) if x["type"] == "mic_level"]
    assert len(levels) == 5 and all(x["rms"] == 0.0 for x in levels)


def test_silent_input_after_3_s_of_zero_samples():
    m = LevelMeter(); m.start(0)
    frames = zero_frames(3.0)
    assert silent(feed_all(m, frames[:299])) == []
    assert silent(m.feed(frames[299], SR, 1, 2.99)) == [{"type": "silent_input", "reason": "zeros"}]
    assert silent(feed_all(m, zero_frames(6.0), t0=3.0)) == []


def test_silent_input_rearms_after_sound():
    m = LevelMeter(); m.start(0)
    msgs = feed_all(m, zero_frames(3.0))
    msgs += feed_all(m, sine_frames(0.1, 0.1), t0=3.0)
    msgs += feed_all(m, zero_frames(3.0), t0=3.1)
    assert silent(msgs) == [{"type": "silent_input", "reason": "zeros"}] * 2


def test_quiet_room_noise_is_never_silent_input():
    m = LevelMeter(); m.start(0)
    msgs = feed_all(m, noise_frames(60.0))
    levels = [x for x in msgs if x["type"] == "mic_level"]
    assert silent(msgs) == [] and len(levels) == 300 and all(x["rms"] > 0 for x in levels)


def test_stereo_frames_are_measured():
    m = LevelMeter(); m.start(0)
    levels = [x for x in feed_all(m, sine_frames(1.0, 0.1, channels=2), channels=2) if x["type"] == "mic_level"]
    assert len(levels) == 5 and all(abs(x["rms"] - 0.0707) < 0.002 for x in levels)


def test_no_frames_for_3_s_is_silent_input():
    m = LevelMeter(); m.start(0)
    assert m.idle(2.9) == []
    assert m.idle(3.0) == [{"type": "silent_input", "reason": "no_audio"}]
    assert m.idle(10) == []


def test_no_audio_timer_restarts_on_each_frame():
    m = LevelMeter(); m.start(0)
    m.feed(sine_frames(0.01, 0.1)[0], SR, 1, 2.0)
    assert m.idle(4.9) == []
    assert m.idle(5.0) == [{"type": "silent_input", "reason": "no_audio"}]


def test_no_audio_and_zeros_share_one_episode():
    m = LevelMeter(); m.start(0)
    assert silent(feed_all(m, zero_frames(3.0))) == [{"type": "silent_input", "reason": "zeros"}]
    assert m.idle(6.5) == [] and m.idle(20) == []
    m.feed(sine_frames(0.01, 0.1)[0], SR, 1, 20.0)
    assert m.idle(23.0) == [{"type": "silent_input", "reason": "no_audio"}]


class _Source(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        await self.push_frame(frame, direction)


class _Sink(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.audio = 0
        self.messages: list[dict] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.audio += 1
        elif isinstance(frame, RTVIServerMessageFrame):
            self.messages.append(frame.data)
        await self.push_frame(frame, direction)


async def test_monitor_pushes_server_message_frames_and_passes_audio():
    src, sink = _Source(), _Sink()
    worker = PipelineWorker(Pipeline([src, InputLevelMonitor(), sink]),
                            params=PipelineParams(audio_in_sample_rate=SR), enable_rtvi=False,
                            cancel_on_idle_timeout=False, idle_timeout_secs=None)

    async def drive():
        await src.started.wait()
        for f in sine_frames(1.0, 0.1):
            await src.push_frame(InputAudioRawFrame(audio=f, sample_rate=SR, num_channels=1))
            await asyncio.sleep(0)
        await asyncio.sleep(0.2)
        await worker.queue_frame(EndFrame())

    driver = asyncio.create_task(drive())
    await asyncio.wait_for(_run(worker), 10)
    await driver
    assert sink.audio == 100
    assert [m["type"] for m in sink.messages] == ["mic_level"] * 5


async def _run(worker):
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()
