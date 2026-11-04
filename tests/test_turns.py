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


# ---- resume merge (AT2)

def _context(notes: bool = False):
    from pipecat.processors.aggregators.llm_context import LLMContext

    a = [{"type": "text", "text": "A"}, {"type": "text", "text": "[Interviewer note, not said by the candidate] x"}] \
        if notes else "A"
    return LLMContext(messages=[
        {"role": "user", "content": "(The candidate has joined the call.)"},
        {"role": "assistant", "content": "Hello Sam. Tell me about yourself."},
        {"role": "user", "content": a},
        {"role": "assistant", "content": "Thanks, and",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "ask_question", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": true}"},
        {"role": "user", "content": "B"},
    ])


def test_resume_within_window_merges_context():
    from iassistpro.turns import ResumeTracker, merge_resumed_turn

    r = ResumeTracker(window_s=2.0)
    r.on_user_stopped(10.0)
    r.on_reply_started()
    assert r.on_user_started(11.5) == 1500
    ctx = _context()
    assert merge_resumed_turn(ctx) is True
    msgs = ctx.get_messages()
    assert len(msgs) == 3 and msgs[-1] == {"role": "user", "content": "A B"}
    ctx = _context(notes=True)
    merge_resumed_turn(ctx)
    last = ctx.get_messages()[-1]
    assert last["content"][0] == {"type": "text", "text": "A B"} and last["content"][1]["text"].startswith("[Interviewer")


def test_resume_after_window_is_a_real_barge_in():
    from iassistpro.turns import ResumeTracker

    r = ResumeTracker(window_s=2.0)
    r.on_user_stopped(10.0)
    r.on_reply_started()
    assert r.on_user_started(12.5) is None            # too late: today's barge-in, context untouched
    r.on_user_stopped(20.0)
    r.on_reply_started()
    r.on_reply_finished()
    assert r.on_user_started(20.5) is None            # the reply was already heard in full: a new answer
    r.on_user_stopped(30.0)
    assert r.on_user_started(30.5) is None            # no reply had started: nothing to cancel


def test_director_does_not_advance_on_a_cancelled_reply(example_brief):
    from iassistpro.contract import Limits, Prices
    from iassistpro.director import InterviewState
    from iassistpro.limits import CostMeter, LimitTracker

    brief = example_brief.model_copy(update={"limits": Limits(max_follow_ups=1, max_minutes=25)})
    st = InterviewState(brief, CostMeter(Prices(input=0.1, cached_input=0.01, cache_write=0.125, output=0.5), 1.0),
                        LimitTracker(brief.limits, [q.id for q in brief.planned_questions]))
    st.on_tool("ask_question", {"question_id": "q1"}, True)
    snap = st.snapshot()
    st.on_tool("follow_up", {"question_id": "q1"}, True)
    st.on_tool("ask_question", {"question_id": "q2"}, True)
    st.restore(snap)
    assert (st.current_question_id, st.current_kind, st.question_number) == ("q1", "main", 1)
    assert st.tracker.follow_ups("q1") == 0 and st.tracker.main_asked == 1


# ---- BARGE_IN_MIN_S: a barge-in floor while the interviewer speaks (UX5, UXP3)

def test_barge_in_floor_defaults_to_off():
    from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy

    from iassistpro.providers import make_turn_strategies

    s = Settings()
    assert s.barge_in_min_s == 0.0
    strategies, _vad = make_turn_strategies(s)
    assert [type(x) for x in strategies.start] == [VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy]


def test_barge_in_floor_setting_selects_the_floor(monkeypatch):
    from pydantic import ValidationError

    from iassistpro.providers import make_turn_strategies
    from iassistpro.turns import BargeInFloorStartStrategy

    monkeypatch.setenv("BARGE_IN_MIN_S", "0.8")
    strategies, _vad = make_turn_strategies(Settings())
    assert [type(x) for x in strategies.start] == [BargeInFloorStartStrategy]
    assert strategies.start[0].min_s == 0.8
    monkeypatch.setenv("BARGE_IN_MIN_S", "4")
    with pytest.raises(ValidationError):
        Settings()


class FloorDriver:
    def __init__(self, min_s: float = 0.6):
        from iassistpro.turns import BargeInFloorStartStrategy

        self.strategy = BargeInFloorStartStrategy(min_s=min_s)
        self.started = 0
        self.resets = 0

        async def on_started(_s, _params):
            self.started += 1
            await self.strategy.handle_user_turn_started()  # what the turn controller does

        async def on_reset(_s):
            self.resets += 1

        self.strategy.add_event_handler("on_user_turn_started", on_started)
        self.strategy.add_event_handler("on_reset_aggregation", on_reset)

    async def setup(self):
        await self.strategy.setup(SimpleNamespace(task_manager=TaskManager(), audio_in_sample_rate=16000))
        return self

    async def send(self, frame):
        await self.strategy.process_frame(frame)

    async def audio(self, seconds: float):
        for _ in range(round(seconds * 100)):
            await self.send(InputAudioRawFrame(audio=bytes(320), sample_rate=16000, num_channels=1))

    async def vad_start(self, start_secs: float = 0.2):
        await self.send(VADUserStartedSpeakingFrame(start_secs=start_secs, timestamp=time.time()))

    async def vad_stop(self):
        await self.send(VADUserStoppedSpeakingFrame(stop_secs=VAD_STOP_S, timestamp=time.time()))


async def test_floor_starts_at_once_when_the_bot_is_silent():
    d = await FloorDriver().setup()
    await d.vad_start()
    assert d.started == 1


async def test_floor_ignores_short_speech_while_the_bot_speaks():
    from pipecat.frames.frames import BotStartedSpeakingFrame

    d = await FloorDriver().setup()
    await d.send(BotStartedSpeakingFrame())
    await d.vad_start(0.2)
    await d.audio(0.3)
    await d.vad_stop()
    assert (d.started, d.resets) == (0, 1)


async def test_floor_interrupts_after_min_s_of_speech():
    from pipecat.frames.frames import BotStartedSpeakingFrame

    d = await FloorDriver(min_s=0.8).setup()
    await d.send(BotStartedSpeakingFrame())
    await d.vad_start(0.2)
    await d.audio(0.59)
    assert d.started == 0
    await d.audio(0.02)
    assert d.started == 1


async def test_floor_drops_the_late_transcript_of_rejected_speech():
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame

    d = await FloorDriver(min_s=0.8).setup()
    await d.send(BotStartedSpeakingFrame())
    await d.vad_start(0.2)
    await d.audio(0.3)   # a 0.5 s echo burst of the interviewer's own voice
    await d.vad_stop()
    await d.send(BotStoppedSpeakingFrame())
    await d.send(TranscriptionFrame("hello", "u", "t"))
    assert d.started == 0 and d.resets >= 2
    await d.vad_start()
    assert d.started == 1
