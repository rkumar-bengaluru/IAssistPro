"""The audio check: an LLM-less call that tests audio both ways (spec §3.1; UX7, UXP2).

transport → level monitor → STT → user aggregator (the interview's turn rules) → CheckController → TTS → output.
The service speaks a fixed sentence; on the browser's `listen` it sends the candidate's next turn back as `heard`.
Nothing is stored, recorded or billed, and no LLM is ever built: this module must not import `make_llm`.
"""

import asyncio
import time
from typing import Literal, Protocol

from pipecat.frames.frames import LLMContextFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIClientMessageFrame, RTVIServerMessageFrame
from pipecat.workers.runner import WorkerRunner

from .config import Settings
from .levels import InputLevelMonitor
from .logs import log_event
from .providers import make_stt, make_tts, make_turn_strategies

CHECK_PROMPT = "This is your interviewer. If you can hear me, press 'I heard it'."
CHECK_MAX_S = 120

CheckEndReason = Literal["ended_by_client", "disconnected", "time_limit", "error"]


class CheckHooks(Protocol):
    check_id: str
    end_requested: asyncio.Event

    def on_live(self) -> None: ...
    def on_end(self, reason: CheckEndReason, error: str | None) -> None: ...


class CheckController(FrameProcessor):
    """Speaks the prompt, answers `listen` / `play_prompt`, and drops the aggregator's context frames (no LLM)."""

    def __init__(self, check_id: str):
        super().__init__()
        self.check_id = check_id
        self.listening = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            return
        if isinstance(frame, RTVIClientMessageFrame):
            if frame.type == "listen":
                self.listening = True
            elif frame.type == "play_prompt":
                await self.play_prompt()
            return
        await self.push_frame(frame, direction)

    async def play_prompt(self) -> None:
        await self.push_frame(TTSSpeakFrame(CHECK_PROMPT))

    async def on_user_turn(self, text: str | None) -> None:
        if not self.listening:
            return
        heard = (text or "").strip()
        self.listening = False
        await self.push_frame(RTVIServerMessageFrame(data={"type": "heard", "text": heard}))
        log_event("check_heard", check=self.check_id, chars=len(heard))

    async def send_ended(self, reason: str) -> None:
        await self.push_frame(RTVIServerMessageFrame(data={"type": "check_ended", "reason": reason}))


async def run_check(hooks: CheckHooks, transport, s: Settings, *, stt=None, tts=None, turn_strategies=None, vad=None,
                    level_monitor: InputLevelMonitor | None = None, max_s: float = CHECK_MAX_S) -> None:
    t0 = time.monotonic()
    stt = stt if stt is not None else make_stt(s)
    tts = tts if tts is not None else make_tts(s)
    if turn_strategies is None:
        turn_strategies, vad = make_turn_strategies(s)
    monitor = level_monitor if level_monitor is not None else InputLevelMonitor()
    controller = CheckController(hooks.check_id)
    aggs = LLMContextAggregatorPair(
        LLMContext(), user_params=LLMUserAggregatorParams(vad_analyzer=vad, user_turn_strategies=turn_strategies))
    user = aggs.user()

    @user.event_handler("on_user_turn_stopped")
    async def _turn(_agg, _strategy, message):
        await controller.on_user_turn(getattr(message, "content", None))

    pipeline = Pipeline([transport.input(), monitor, stt, user, controller, tts, transport.output()])
    worker = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
                            cancel_on_idle_timeout=False, idle_timeout_secs=None)
    reason: list[CheckEndReason] = []

    async def finish(r: CheckEndReason, *, tell: bool) -> None:
        if reason:
            return
        reason.append(r)
        if tell:
            await controller.send_ended(r)
            await asyncio.sleep(0.2)  # let the message reach the data channel before the connection drops
        await worker.cancel()

    @transport.event_handler("on_client_connected")
    async def _connected(_t, _client):
        log_event("check_connected", check=hooks.check_id, setup_ms=int(1000 * (time.monotonic() - t0)))
        await monitor.start_watchdog()
        hooks.on_live()
        await controller.play_prompt()

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_t, _client):
        await finish("disconnected", tell=False)

    async def watch_end_request():
        await hooks.end_requested.wait()
        await finish("ended_by_client", tell=True)

    async def time_limit():  # counted from the offer (contract: "120 s after the offer"), so a never-connected
        await asyncio.sleep(max_s)  # check ends too
        await finish("time_limit", tell=True)

    watchers = [asyncio.create_task(watch_end_request()), asyncio.create_task(time_limit())]
    error: str | None = None
    try:
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
    except Exception as e:  # logged by type only: messages can carry content
        error = f"pipeline_error:{type(e).__name__}"
        log_event("pipeline_error", error=type(e).__name__)
    finally:
        for w in watchers:
            w.cancel()
        await monitor.stop_watchdog()
        final: CheckEndReason = "error" if error else (reason[0] if reason else "disconnected")
        log_event("check_end", check=hooks.check_id, reason=final, ms=int(1000 * (time.monotonic() - t0)))
        hooks.on_end(final, error)
