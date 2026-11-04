"""Inbound mic level and silent-input detection (spec §3.1 step 2, §3.3; UX3, UXP2).

`LevelMeter` is pure: int16 PCM in, `mic_level` / `silent_input` messages out. Silence means *digital* silence
(every sample exactly 0) or no frames at all; a quiet room (±1–2 LSB) is never silent, so a candidate thinking quietly
is never nagged. `InputLevelMonitor` puts the meter in a pipeline right after `transport.input()` and pushes its
messages as RTVI server messages (Pipecat's RTVI observer forwards them on the data channel).
"""

import asyncio
import time
from typing import Callable

import numpy as np
from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame

from .logs import log_event

LEVEL_WINDOW_S = 0.2
SILENT_AFTER_S = 3.0


class LevelMeter:
    def __init__(self, *, window_s: float = LEVEL_WINDOW_S, silent_after_s: float = SILENT_AFTER_S):
        self.window_s = window_s
        self.silent_after_s = silent_after_s
        self._sumsq = 0.0
        self._n = 0
        self._zero_run_s = 0.0
        self._armed = True
        self._started = False
        self._last = 0.0

    def start(self, now: float) -> None:
        self._started = True
        self._last = now

    def feed(self, pcm: bytes, sample_rate: int, channels: int, now: float) -> list[dict]:
        samples = np.frombuffer(pcm, dtype="<i2")
        if samples.size == 0:
            return []
        self._last = now
        out: list[dict] = []
        x = samples.astype(np.float64)
        self._sumsq += float(np.dot(x, x))
        self._n += samples.size
        if self._n >= self.window_s * sample_rate * channels:
            rms = float(np.sqrt(self._sumsq / self._n)) / 32768
            # 4 decimals, but a window with any non-zero sample never rounds down to 0.0: exactly 0.0 means
            # digital silence, and the browser clears its warning on rms > 0 (a quiet room is ~0.00004).
            out.append({"type": "mic_level", "rms": max(round(rms, 4), 0.0001) if self._sumsq > 0 else 0.0})
            self._sumsq, self._n = 0.0, 0
        if np.any(samples):
            self._zero_run_s = 0.0
            self._armed = True
        else:
            self._zero_run_s += samples.size / (sample_rate * channels)
            if self._armed and self._zero_run_s >= self.silent_after_s - 1e-9:
                self._armed = False
                out.append({"type": "silent_input", "reason": "zeros"})
        return out

    def idle(self, now: float) -> list[dict]:
        if self._started and self._armed and now - self._last >= self.silent_after_s - 1e-9:
            self._armed = False
            return [{"type": "silent_input", "reason": "no_audio"}]
        return []


class InputLevelMonitor(FrameProcessor):
    """Passes every frame through; turns inbound audio into RTVI server messages; watches for no audio at all."""

    def __init__(self, meter: LevelMeter | None = None, clock: Callable[[], float] = time.monotonic,
                 poll_s: float = 0.5):
        super().__init__()
        self.meter = meter or LevelMeter()
        self.clock = clock
        self.poll_s = poll_s
        self._watchdog: asyncio.Task | None = None

    async def _send(self, msgs: list[dict]) -> None:
        for m in msgs:
            if m["type"] == "silent_input":
                log_event("silent_input", reason=m["reason"])
            await self.push_frame(RTVIServerMessageFrame(data=m))

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            await self._send(self.meter.feed(frame.audio, frame.sample_rate, frame.num_channels, self.clock()))
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.stop_watchdog()

    async def start_watchdog(self) -> None:
        self.meter.start(self.clock())
        if self._watchdog is None:
            self._watchdog = self.create_task(self._watch())

    async def stop_watchdog(self) -> None:
        if self._watchdog is not None:
            task, self._watchdog = self._watchdog, None
            await self.cancel_task(task)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            await self._send(self.meter.idle(self.clock()))
