"""In-process harness for the interview pipeline: no audio, no network.

The user side is fed as VAD/UserStarted → Transcription → VAD/UserStopped frames from a fake transport input,
with Pipecat's external user-turn strategies (the aggregator adopts the turn frames). A fake TTS turns LLM text
into TTSStarted/TTSText/TTSStopped, and the fake output turns those into Bot(Started|Stopped)SpeakingFrames.
"""

import asyncio
import uuid
from dataclasses import dataclass, field

from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, CancelFrame, EndFrame, InputAudioRawFrame, InterruptionFrame,
    LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, StartFrame, TranscriptionFrame,
    TTSSpeakFrame, TTSStartedFrame, TTSStoppedFrame, TTSTextFrame, UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIClientMessageFrame, RTVIServerMessageFrame
from pipecat.turns.user_start.external_user_turn_start_strategy import ExternalUserTurnStartStrategy
from pipecat.turns.user_stop.external_user_turn_stop_strategy import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601

from iassistpro.config import Settings
from iassistpro.contract import AudioInfo, Brief, Latency
from iassistpro.fake_llm import FakeInterviewerLLM
from iassistpro.levels import InputLevelMonitor
from iassistpro.pipeline import run_interview
from iassistpro.store import SessionMeta, SessionStore, StateRecord


class Passthrough(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class FakeInput(Passthrough):
    def __init__(self):
        super().__init__()
        self.start_event = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.start_event.set()

    async def say(self, text: str, *, interrupt: bool = False) -> None:
        if interrupt:  # with external turn strategies the aggregator leaves interruptions to the turn source
            await self.broadcast_interruption()
        await self.push_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        await self.push_frame(UserStartedSpeakingFrame())
        await asyncio.sleep(0.05)
        await self.push_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
        await self.push_frame(TranscriptionFrame(text, "user", time_now_iso8601()))
        await asyncio.sleep(0.02)
        await self.push_frame(UserStoppedSpeakingFrame())

    async def audio(self, seconds: float, amplitude: float | None) -> None:
        """Push 10 ms 16 kHz mono frames: a 440 Hz sine of that full-scale amplitude, or all zeros (None)."""
        import math
        import struct

        for f in range(round(seconds * 100)):
            if amplitude is None:
                pcm = bytes(320)
            else:
                pcm = struct.pack("<160h", *(int(amplitude * 32767 * math.sin(2 * math.pi * 440 * (f * 160 + i) / 16000))
                                             for i in range(160)))
            await self.push_frame(InputAudioRawFrame(audio=pcm, sample_rate=16000, num_channels=1))
            await asyncio.sleep(0)

    async def client_message(self, type: str) -> None:
        await self.push_frame(RTVIClientMessageFrame(msg_id=uuid.uuid4().hex, type=type, data=None))


class FakeTTS(FrameProcessor):
    """LLM text or TTSSpeakFrame → TTSStarted, TTSText per chunk, TTSStopped at the response end."""

    def __init__(self, speak_s: float = 0.05):
        super().__init__()
        self.speak_s = speak_s
        self._tts_speaking = False
        self._gen = 0  # bumped by an interruption: a reply's pending stop must not end the next reply

    async def _tts_on(self):
        if not self._tts_speaking:
            self._tts_speaking = True
            await self.push_frame(TTSStartedFrame())

    async def _tts_off(self):
        if self._tts_speaking:
            self._tts_speaking = False
            gen = self._gen
            await asyncio.sleep(self.speak_s)
            if gen == self._gen:
                await self.push_frame(TTSStoppedFrame())

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMTextFrame):
            await self._tts_on()
            await self.push_frame(TTSTextFrame(text=frame.text, text_type="sentence"))
            return
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSSpeakFrame):
            await self.push_frame(frame)
            await self._tts_on()
            await self.push_frame(TTSTextFrame(text=frame.text, text_type="sentence"))
            await self._tts_off()
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            await self.push_frame(frame, direction)
            await self._tts_off()
            return
        if isinstance(frame, InterruptionFrame):
            self._tts_speaking = False
            self._gen += 1
        await self.push_frame(frame, direction)


class FakeOutput(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.bot_stops = 0
        self.stop_event = asyncio.Event()
        self.bot_speaking = False
        self.server_messages: list[dict] = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, RTVIServerMessageFrame):
            self.server_messages.append(frame.data)
        if isinstance(frame, TTSStartedFrame) and not self.bot_speaking:
            self.bot_speaking = True
            await self.push_frame(BotStartedSpeakingFrame())
            await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        elif isinstance(frame, (TTSStoppedFrame, InterruptionFrame)) and self.bot_speaking:
            self.bot_speaking = False
            await self.push_frame(BotStoppedSpeakingFrame())
            await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            self.bot_stops += 1
            self.stop_event.set()


class FakeTransport:
    def __init__(self):
        self._in, self._out = FakeInput(), FakeOutput()
        self._handlers: dict[str, list] = {}

    def input(self):
        return self._in

    def output(self):
        return self._out

    def event_handler(self, name):
        def deco(fn):
            self._handlers.setdefault(name, []).append(fn)
            return fn
        return deco

    async def fire(self, name):
        for fn in self._handlers.get(name, []):
            await fn(self, "client")


@dataclass
class FakeHooks:
    brief: Brief
    store: SessionStore
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    end_requested: asyncio.Event = field(default_factory=asyncio.Event)
    latency: Latency = field(default_factory=Latency)
    live: bool = False
    end_reason: str | None = None
    error: str | None = None
    audio: AudioInfo | None = None
    progress: list = field(default_factory=list)

    def on_live(self):
        self.live = True
        rec = self.store.read_state(self.session_id)
        self.store.write_state(self.session_id, rec.model_copy(update={"state": "live"}))

    def on_progress(self, qid, kind, number):
        self.progress.append((qid, kind, number))

    def on_end(self, reason, error):
        self.end_reason, self.error = reason, error
        rec = self.store.read_state(self.session_id)
        self.store.write_state(self.session_id, rec.model_copy(update={
            "state": "ended", "end_reason": reason, "error": error, "latency": self.latency,
            "ended_at": rec.started_at}))

    def on_audio(self, info):
        self.audio = info


def external_strategies():
    return UserTurnStrategies(start=[ExternalUserTurnStartStrategy()], stop=[ExternalUserTurnStopStrategy()])


async def run_scripted(brief: Brief, store: SessionStore, answers: list, *, tool_first_no_text=False,
                       end_after: int | None = None, disconnect_after: int | None = None, timeout=20.0,
                       tts_speak_s: float = 0.05, llm=None, on_connected=None, monitor_clock=None):
    """Answer each interviewer reply in turn; optionally press End or disconnect after N answers.

    An answer given as a tuple of fragments ends its turn after each fragment; the next fragment starts as soon as
    the interviewer starts replying (a candidate resuming after a pause the turn detection took for the end).
    """
    from datetime import datetime, timezone

    hooks = FakeHooks(brief, store)
    store.create(hooks.session_id, SessionMeta.from_brief(brief, datetime.now(timezone.utc)))
    store.write_state(hooks.session_id, StateRecord(state="waiting", started_at=datetime.now(timezone.utc),
                                                    audio=AudioInfo(state="pending")))
    transport = FakeTransport()
    llm = llm if llm is not None else FakeInterviewerLLM(brief, tool_first_no_text=tool_first_no_text)
    s = Settings(llm_provider="fake")
    monitor = InputLevelMonitor(clock=monitor_clock, poll_s=0.01) if monitor_clock is not None else None
    task = asyncio.create_task(run_interview(hooks, transport, s, stt=Passthrough(), tts=FakeTTS(tts_speak_s), llm=llm,
                                             turn_strategies=external_strategies(), vad=None, close_timeout_s=3.0,
                                             level_monitor=monitor))
    out = transport.output()

    async def drive():
        await transport.input().start_event.wait()
        await transport.fire("on_client_connected")
        if on_connected is not None:
            await on_connected(transport)
        for i, a in enumerate(answers):
            await asyncio.wait_for(out.stop_event.wait(), 5)
            out.stop_event.clear()
            await asyncio.sleep(0.1)
            if task.done():
                return
            if isinstance(a, tuple):
                for frag in a[:-1]:
                    await transport.input().say(frag)
                    for _ in range(300):
                        if out.bot_speaking:
                            break
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.05)
                await transport.input().say(a[-1], interrupt=True)
                await asyncio.sleep(0.05)
                out.stop_event.clear()   # the interrupted reply's stop; wait for the merged turn's reply
            else:
                await transport.input().say(a)
            if end_after is not None and i + 1 == end_after:
                await asyncio.sleep(0.01)
                hooks.end_requested.set()
                return
            if disconnect_after is not None and i + 1 == disconnect_after:
                await asyncio.sleep(0.3)
                await transport.fire("on_client_disconnected")
                return

    driver = asyncio.create_task(drive())
    await asyncio.wait_for(task, timeout)
    driver.cancel()
    hooks.llm = llm
    hooks.server_messages = out.server_messages
    return hooks


class ToolLoopLLM(FakeInterviewerLLM):
    """A model stuck calling tools without ever speaking. Usage is reported before the tool call, so the
    director's budget check sees this run's cost when the tool result asks for the re-run."""

    def __init__(self, brief: Brief, *, on_run=None, **kwargs):
        super().__init__(brief, **kwargs)
        self.on_run = on_run or (lambda runs: None)

    async def _respond(self, context) -> None:
        from pipecat.frames.frames import FunctionCallFromLLM
        from pipecat.metrics.metrics import LLMTokenUsage

        self.runs += 1
        self.on_run(self.runs)
        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_llm_usage_metrics(LLMTokenUsage(
            prompt_tokens=200, completion_tokens=40, total_tokens=1240, cache_read_input_tokens=1000,
            cache_creation_input_tokens=0))
        qid = self.brief.planned_questions[0].id
        await self.run_function_calls([FunctionCallFromLLM(function_name="ask_question", tool_call_id=uuid.uuid4().hex,
                                                           arguments={"question_id": qid}, context=context)])
        await self.push_frame(LLMFullResponseEndFrame())
