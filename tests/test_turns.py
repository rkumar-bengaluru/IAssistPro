"""Turn-taking: the silence grace before a turn ends (AT1).

The tests drive Pipecat's real TurnAnalyzerUserTurnStopStrategy with a scripted turn analyzer, on a virtual clock
advanced by 10 ms audio frames, so every time below is exact and nothing sleeps.
"""

import time
from types import SimpleNamespace

import pytest
from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer, BaseTurnParams, EndOfTurnState
from pipecat.frames.frames import (
    InputAudioRawFrame, STTMetadataFrame, TranscriptionFrame, VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.utils.asyncio.task_manager import TaskManager

from iassistpro.config import Settings
from iassistpro.turns import GraceUserTurnStopStrategy

FRAME_MS = 10
VAD_STOP_S = 0.2
FALLBACK_SILENCE_MS = 3000  # smart-turn's own stop_secs fallback


class ScriptedAnalyzer(BaseTurnAnalyzer):
    """Says COMPLETE at the scripted times, else INCOMPLETE; COMPLETE after 3 s of silence as smart-turn does."""

    def __init__(self, clock, complete_at_ms: list[int]):
        super().__init__(sample_rate=16000)
        self.clock, self.complete_at = clock, sorted(complete_at_ms)
        self._silence_ms, self._triggered, self._fallback_done = 0, False, False

    @property
    def speech_triggered(self) -> bool:
        return self._triggered

    @property
    def params(self) -> BaseTurnParams:
        return BaseTurnParams()

    def _due(self) -> bool:
        if self.complete_at and self.clock.ms >= self.complete_at[0]:
            self.complete_at.pop(0)
            return True
        return False

    def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
        if is_speech:
            self._silence_ms, self._triggered, self._fallback_done = 0, True, False
            return EndOfTurnState.INCOMPLETE
        self._silence_ms += FRAME_MS
        if self._due():
            self.complete_at.insert(0, self.clock.ms)  # analyze_end_of_turn consumes it
            return EndOfTurnState.COMPLETE
        if self._triggered and not self._fallback_done and self._silence_ms >= FALLBACK_SILENCE_MS:
            self._fallback_done = True
            self.complete_at.insert(0, self.clock.ms)
            return EndOfTurnState.COMPLETE
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self):
        return (EndOfTurnState.COMPLETE if self._due() else EndOfTurnState.INCOMPLETE), None

    def clear(self):
        self._silence_ms, self._triggered = 0, False


async def run_script(events: dict[int, list[tuple]], grace_s: float, end_ms: int, complete_at_ms: list[int]):
    """Feed 10 ms frames from -1000 ms to end_ms; events[t] are applied at t. Returns the turn-stop times (ms)."""
    clock = SimpleNamespace(ms=-1010)
    inner = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=ScriptedAnalyzer(clock, complete_at_ms))
    strategy = GraceUserTurnStopStrategy(inner, grace_s=grace_s)
    stops: list[int] = []

    async def on_stopped(_strategy, _params):
        stops.append(clock.ms)

    strategy.add_event_handler("on_user_turn_stopped", on_stopped)
    await strategy.setup(SimpleNamespace(task_manager=TaskManager(), audio_in_sample_rate=16000))
    await strategy.process_frame(STTMetadataFrame(service_name="stt", ttfs_p99_latency=4.0))
    await strategy.handle_user_turn_started()
    async def feed(frame):
        before = len(stops)
        await strategy.process_frame(frame)
        if len(stops) > before:
            await strategy.handle_user_turn_stopped()  # what the turn controller does

    while clock.ms < end_ms:
        clock.ms += FRAME_MS
        # the audio frame covering (t - 10 ms, t], then the events at t
        await feed(InputAudioRawFrame(audio=bytes(320), sample_rate=16000, num_channels=1))
        for ev in events.get(clock.ms, []):
            if ev[0] == "speech_start":
                await feed(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time()))
            elif ev[0] == "vad_stop":  # VAD fires stop_secs after the last speech
                await feed(VADUserStoppedSpeakingFrame(stop_secs=VAD_STOP_S, timestamp=time.time()))
            elif ev[0] == "transcript":
                f = TranscriptionFrame(ev[1], "u", "t")
                f.finalized = ev[2]
                await feed(f)
    await strategy.cleanup()
    return stops


def one_answer(transcript_ms: int) -> dict:
    # speech -1000..0, VAD stop event at 200 ms (0.2 s after the last speech)
    return {-1000: [("speech_start",)], 200: [("vad_stop",)], transcript_ms: [("transcript", "answer", True)]}


async def test_complete_verdict_waits_for_the_grace():
    assert await run_script(one_answer(1000), 1.5, 4000, complete_at_ms=[250]) == [1500]


async def test_speech_during_grace_cancels_the_end():
    events = {-1000: [("speech_start",)], 200: [("vad_stop",)], 900: [("speech_start",)],
              1000: [("transcript", "first part", False)], 5200: [("vad_stop",)],
              6000: [("transcript", "second part", True)]}
    # speech resumes at 0.9 s and stops at 5.0 s (VAD stop event at 5.2 s)
    assert await run_script(events, 1.5, 9000, complete_at_ms=[250, 5250]) == [6500]


async def test_whisper_slower_than_grace_ends_on_transcript():
    assert await run_script(one_answer(2200), 1.5, 4000, complete_at_ms=[250]) == [2200]


async def test_incomplete_verdict_still_uses_the_fallback():
    with_grace = await run_script(one_answer(1000), 1.5, 6000, complete_at_ms=[])
    without = await run_script(one_answer(1000), 0.0, 6000, complete_at_ms=[])
    assert with_grace == without and len(with_grace) == 1 and with_grace[0] >= 3000


async def test_grace_zero_is_todays_behaviour():
    assert await run_script(one_answer(1000), 0.0, 4000, complete_at_ms=[250]) == [1000]


def test_settings_read_grace_and_window(monkeypatch):
    s = Settings()
    assert (s.turn_grace_s, s.resume_window_s) == (1.5, 2.0)
    monkeypatch.setenv("TURN_GRACE_S", "5.1")
    with pytest.raises(ValueError):
        Settings()
    monkeypatch.setenv("TURN_GRACE_S", "2.0")
    monkeypatch.setenv("RESUME_WINDOW_S", "0")
    assert (Settings().turn_grace_s, Settings().resume_window_s) == (2.0, 0.0)
