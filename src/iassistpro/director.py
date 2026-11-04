"""Interview state (pure) and the director processor that enforces limits around the LLM.

The interviewer signals structure with tool calls spoken in the same reply as the text (§5.4, AS5), so tool
results never re-run the LLM, except when a reply spoke nothing (otherwise the candidate would sit in silence).
Such a re-run is pushed upstream by the assistant aggregator; RerunGate (after the LLM) hands it to the director,
which applies the same hard stops, notes and run count as to any other run, and ends a silent tool loop (AF3).
Notes to the model are appended as an extra text block on the last user message (append-only: caching holds).
"""

import asyncio
import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pipecat.processors.frame_processor import FrameProcessor

from .contract import Brief, EndReason
from .limits import (
    CLOSE_BUDGET, CLOSE_ENDED, CLOSE_NO_SPEECH, CLOSE_TIME, NOTE_BUDGET, NOTE_LAST_MAIN, NOTE_NO_MORE_FOLLOW_UPS, NOTE_PREFIX,
    NOTE_TIME_UP, CostMeter, LimitTracker,
)
from .logs import log_event

TOOL_REASON_MAP: dict[str, EndReason] = {
    "completed": "completed", "candidate_request": "ended_by_candidate", "time_limit": "time_limit",
    "budget": "budget",
}
MAX_SILENT_RERUNS = 2  # consecutive tool-only re-runs with no speech before the fixed closing (AF3)


@dataclass
class ToolOutcome:
    run_llm: bool
    result: dict = field(default_factory=dict)


class InterviewState:
    def __init__(self, brief: Brief, meter: CostMeter, tracker: LimitTracker):
        self.brief = brief
        self.meter = meter
        self.tracker = tracker
        self.planned_ids = [q.id for q in brief.planned_questions]
        self.current_question_id: str | None = None
        self.current_kind: Literal["greeting", "main", "follow_up", "closing"] = "greeting"
        self.question_number = 0
        self.ending = False
        self.end_reason: EndReason | None = None
        self.end_requested = False
        self.overruns = 0
        self.unknown_ids = 0
        self._tool_reason: EndReason | None = None
        self._wrap_reason: EndReason | None = None
        self._hard_reason: EndReason | None = None
        self._disconnected = False
        self._error = False
        self.error_code: str | None = None
        self._notes_sent: set[str] = set()

    # ---- resume merge (AT2): a reply cancelled by a resumed answer must leave no trace in the state
    _SNAPSHOT_SKIP = ("brief", "meter", "planned_ids", "_notes_sent")

    def snapshot(self) -> dict:
        return {k: copy.deepcopy(v) for k, v in self.__dict__.items() if k not in self._SNAPSHOT_SKIP}

    def restore(self, snap: dict) -> None:
        # Notes already sent stay sent (they are in the context); spend stays spent.
        for k, v in snap.items():
            setattr(self, k, copy.deepcopy(v))

    # ---- tools
    def on_tool(self, name: str, args: dict, response_had_text: bool) -> ToolOutcome:
        run_llm = not response_had_text
        if name in ("ask_question", "follow_up"):
            qid = args.get("question_id")
            if qid not in self.planned_ids:
                self.unknown_ids += 1
                log_event("tool_unknown_question_id", tool=name, count=self.unknown_ids)
                return ToolOutcome(run_llm, {"ok": False, "error": "unknown_question_id"})
            if name == "ask_question":
                self.tracker.on_ask(qid)
                self.current_kind = "main"
                self.question_number = self.tracker.main_asked
            else:
                if self.tracker.follow_up_limit_reached(qid):
                    self.overruns += 1
                    log_event("follow_up_overrun", overruns=self.overruns)
                self.tracker.on_follow_up(qid)
                self.current_kind = "follow_up"
            self.current_question_id = qid
            return ToolOutcome(run_llm, {"ok": True})
        if name == "end_interview":
            reason = TOOL_REASON_MAP.get(str(args.get("reason")), "completed")
            self._tool_reason = reason
            self.ending = True
            self.current_question_id, self.current_kind = None, "closing"
            self.end_reason = self.final_end_reason()
            return ToolOutcome(run_llm, {"ok": True})
        log_event("tool_unknown", tool="other")
        return ToolOutcome(False, {"ok": False, "error": "unknown_tool"})

    # ---- limits
    def _note(self, key: str, text: str, out: list[str]) -> None:
        if key not in self._notes_sent:
            self._notes_sent.add(key)
            out.append(NOTE_PREFIX + text)

    def notes_due(self, elapsed_s: float) -> list[str]:
        out: list[str] = []
        if self.ending:
            return out
        for qid in self.planned_ids:
            if self.tracker.follow_ups(qid) and self.tracker.follow_up_limit_reached(qid):
                self._note(f"fu:{qid}", NOTE_NO_MORE_FOLLOW_UPS.format(qid=qid), out)
        if self.tracker.last_main_asked():
            self._note("last_main", NOTE_LAST_MAIN, out)
        if self.tracker.time_state(elapsed_s) != "ok":
            self._wrap_reason = self._wrap_reason or "time_limit"
            self._note("time", NOTE_TIME_UP, out)
        if self.meter.near_cap():
            self._wrap_reason = self._wrap_reason or "budget"
            self._note("budget", NOTE_BUDGET, out)
        return out

    def hard_stop_due(self, elapsed_s: float, *, include_budget: bool = True) -> Literal["time_limit", "budget"] | None:
        if self.ending:
            return None
        if include_budget and self.meter.next_turn_would_exceed():
            return "budget"
        if self.tracker.time_state(elapsed_s) == "hard_stop":
            return "time_limit"
        return None

    def mark_hard_stop(self, reason: EndReason, error: str | None = None) -> None:
        self._hard_reason = reason
        self.error_code = error
        self.ending = True
        self.current_question_id, self.current_kind = None, "closing"
        self.end_reason = self.final_end_reason()

    # ---- endings
    def request_end(self) -> None:
        self.end_requested = True
        self.ending = True
        self.current_question_id, self.current_kind = None, "closing"
        self.end_reason = self.final_end_reason()

    def on_disconnect(self) -> None:
        self._disconnected = True

    def on_error(self) -> None:
        self._error = True

    def final_end_reason(self) -> EndReason:
        if self.end_requested:
            return "ended_by_candidate"
        if self._hard_reason:
            return self._hard_reason
        if self.ending:
            return self._wrap_reason or self._tool_reason or "completed"
        if self._error:
            return "error"
        if self._disconnected:
            return "disconnected"
        return "error"


def append_notes(context, notes: list[str]) -> None:
    """Append each note as an extra text block on the last user message (or a new user message)."""
    if not notes:
        return
    messages = context.get_messages()
    blocks = [{"type": "text", "text": n} for n in notes]
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content}] + blocks
                return
            if isinstance(content, list):
                content.extend(blocks)
                return
        break  # the last message is not a user message: add one (the adapter merges same-role messages)
    context.add_message({"role": "user", "content": blocks})


class SpokenTextProbe:
    """Whether the current LLM response pushed non-blank text before its function call.

    It hooks the LLM's synchronous on_before_push_frame event rather than an observer, because observers are
    fed through queues and could lag behind the tool call.
    """

    def __init__(self):
        self.had_text = False
        self.in_response = False

    def on_frame(self, frame) -> None:
        from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame

        if isinstance(frame, LLMFullResponseStartFrame):
            self.had_text = False
            self.in_response = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.in_response = False
        elif isinstance(frame, LLMTextFrame) and frame.text and frame.text.strip():
            self.had_text = True

    def attach(self, llm) -> None:
        @llm.event_handler("on_before_push_frame")
        async def _on_push(_proc, frame):
            self.on_frame(frame)


class InterviewDirector(FrameProcessor):
    """Sits between the user aggregator and the LLM; owns the tool handlers, notes, fixed closings and the end."""

    def __init__(self, state: InterviewState, llm, *, elapsed: Callable[[], float],
                 on_label: Callable[[str | None, str], None] | None = None,
                 on_progress: Callable[[str | None, str, int], None] | None = None,
                 close_timeout_s: float = 15.0, settle_s: float = 0.7, tick_s: float = 1.0,
                 interrupt_settle_s: float = 0.3, resume_window_s: float = 0.0, **kwargs):
        super().__init__(**kwargs)
        from .turns import ResumeTracker

        self.resume = ResumeTracker(resume_window_s)
        self.resumes = 0
        self._snapshot: dict | None = None
        self._silent_reruns = 0
        self._merge_next = False
        self.state = state
        self.llm = llm
        self.elapsed = elapsed
        self.on_label = on_label or (lambda q, k: None)
        self.on_progress = on_progress or (lambda q, k, n: None)
        self.close_timeout_s = close_timeout_s
        self.settle_s = settle_s
        self.tick_s = tick_s
        self.interrupt_settle_s = interrupt_settle_s
        self.probe = SpokenTextProbe()
        self.probe.attach(llm)
        self.llm_runs = 0
        self._bot_speaking = False
        self._user_speaking = False
        self._closing_spoke = False
        self._last_bot_stop = 0.0
        self._end_task = None
        self._timer_task = None
        self._ended = False
        for name in ("ask_question", "follow_up", "end_interview"):
            llm.register_function(name, self._handle_tool)

    @property
    def name_for_closing(self) -> str:
        return self.state.brief.candidate.name

    async def _handle_tool(self, params) -> None:
        from pipecat.frames.frames import FunctionCallResultProperties

        outcome = self.state.on_tool(params.function_name, dict(params.arguments or {}), self.probe.had_text)
        if outcome.run_llm:
            log_event("tool_without_text", tool=params.function_name)
        await self._label(self.state.current_question_id, self.state.current_kind, via_llm=True)
        self.on_progress(self.state.current_question_id, self.state.current_kind, self.state.question_number)
        if self.state.ending:
            self._start_ending(fresh=False)
        await params.result_callback(outcome.result, properties=FunctionCallResultProperties(run_llm=outcome.run_llm))

    async def _label(self, question_id, kind, *, via_llm: bool = False) -> None:
        """Put the label in the frame stream (in order with the reply it belongs to) and tell on_label."""
        from .transcript import TurnLabelFrame

        self.on_label(question_id, kind)
        frame = TurnLabelFrame(question_id=question_id, kind=kind)
        if via_llm:
            await self.llm.push_frame(frame)
        else:
            await self.push_frame(frame)

    # ---- frames
    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame, BotStoppedSpeakingFrame, CancelFrame, EndFrame, LLMContextFrame, StartFrame,
            UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
        )
        from pipecat.processors.frame_processor import FrameDirection

        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._timer_task = self.create_task(self._timer())
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._stop_tasks()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self.state.ending:
                log_event("llm_run_dropped_while_ending")
                return
            el = self.elapsed()
            hard = self.state.hard_stop_due(el)
            if hard:
                await self._fixed_close(hard)
                return
            if self._merge_next:
                from .turns import merge_resumed_turn

                self._merge_next = False
                merge_resumed_turn(frame.context)
            self._snapshot = self.state.snapshot()
            self.resume.on_reply_started()
            self._silent_reruns = 0
            self._add_notes(frame.context, el)
            self.llm_runs += 1
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._silent_reruns = 0
            if self.state.ending:
                self._closing_spoke = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._last_bot_stop = time.monotonic()
            if not self.probe.in_response:
                self.resume.on_reply_finished()
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
            gap = self.resume.on_user_started(self.elapsed())
            await self.push_frame(frame, direction)
            if gap is not None and not self.state.ending and self._snapshot is not None:
                await self._on_resume(gap)
            return
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
            self.resume.on_user_stopped(self.elapsed())
        await self.push_frame(frame, direction)

    def _add_notes(self, context, elapsed_s: float) -> None:
        notes = self.state.notes_due(elapsed_s)
        if notes:
            append_notes(context, notes)
            log_event("notes_added", n=len(notes))

    async def allow_rerun(self, frame) -> bool:
        """A tool result asked for another LLM run (pushed upstream past the director): same checks as a turn."""
        if self.state.ending:
            log_event("llm_run_dropped_while_ending")
            return False
        el = self.elapsed()
        hard = self.state.hard_stop_due(el)
        if hard:
            await self._fixed_close(hard)
            return False
        self._silent_reruns += 1
        if self._silent_reruns > MAX_SILENT_RERUNS:
            await self._fixed_close("error", error="no_speech_loop")
            return False
        self._add_notes(frame.context, el)
        self.llm_runs += 1
        log_event("llm_rerun", n=self._silent_reruns)
        return True

    async def _on_resume(self, gap_ms: int) -> None:
        """The candidate went on with the answer whose turn just ended: undo the cancelled reply (AT2)."""
        from .transcript import TurnResumedFrame

        self.state.restore(self._snapshot)
        self._snapshot = None
        self._merge_next = True
        self.resumes += 1
        log_event("turn_resumed", gap_ms=gap_ms)
        await self.push_frame(TurnResumedFrame(gap_ms=gap_ms))
        await self._label(self.state.current_question_id, self.state.current_kind)
        self.on_progress(self.state.current_question_id, self.state.current_kind, self.state.question_number)

    async def _fixed_close(self, reason, error: str | None = None) -> None:
        from pipecat.frames.frames import TTSSpeakFrame

        self.state.mark_hard_stop(reason, error)
        text = {"time_limit": CLOSE_TIME, "budget": CLOSE_BUDGET}.get(reason, CLOSE_NO_SPEECH)
        text = text.format(name=self.name_for_closing)
        log_event("hard_stop", reason=reason, **({"error": error} if error else {}))
        await self._label(None, "closing")
        self.on_progress(None, "closing", self.state.question_number)
        self._start_ending(fresh=True)
        await self.push_frame(TTSSpeakFrame(text))

    async def request_end(self) -> None:
        """The candidate pressed End: stop the current reply and speak the fixed closing."""
        from pipecat.frames.frames import TTSSpeakFrame

        if self.state.end_requested:
            return
        was_ending = self.state.ending
        self.state.request_end()
        log_event("end_requested", was_ending=was_ending)
        if was_ending:
            return
        await self.broadcast_interruption()
        # Let the interruption flush a reply that was already in flight, so the closing is heard on its own.
        await asyncio.sleep(self.interrupt_settle_s)
        await self._label(None, "closing")
        self.on_progress(None, "closing", self.state.question_number)
        self._start_ending(fresh=True)
        await self.push_frame(TTSSpeakFrame(CLOSE_ENDED.format(name=self.name_for_closing)))

    def _start_ending(self, *, fresh: bool) -> None:
        if self._end_task is not None:
            return
        self._closing_spoke = False if fresh else self._bot_speaking
        self._end_task = self.create_task(self._end_watch())

    def _closing_done(self) -> bool:
        return (self._closing_spoke and not self._bot_speaking and not self.probe.in_response
                and time.monotonic() - self._last_bot_stop >= self.settle_s)

    async def _end_watch(self) -> None:
        from pipecat.frames.frames import EndWorkerFrame
        from pipecat.processors.frame_processor import FrameDirection

        deadline = time.monotonic() + self.close_timeout_s
        while not self._closing_done():
            if time.monotonic() >= deadline:
                log_event("closing_timeout")
                break
            await asyncio.sleep(0.05)
        self._ended = True
        await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    async def _timer(self) -> None:
        # Time is also enforced while nobody speaks. Budget is checked only before an LLM run, so a candidate
        # always gets to answer the question just asked.
        while True:
            await asyncio.sleep(self.tick_s)
            if self.state.ending or self._bot_speaking or self._user_speaking or self.probe.in_response:
                continue
            hard = self.state.hard_stop_due(self.elapsed(), include_budget=False)
            if hard:
                await self._fixed_close(hard)

    async def _stop_tasks(self) -> None:
        for t in (self._timer_task, self._end_task):
            if t is not None and not t.done():
                await self.cancel_task(t)
        self._timer_task = self._end_task = None

    async def cleanup(self):
        await self._stop_tasks()
        await super().cleanup()


class RerunGate(FrameProcessor):
    """Between the LLM and TTS. Pipecat's assistant aggregator pushes a tool result's re-run (run_llm=True) as an
    upstream LLMContextFrame that never passes the director; this hands it to the director first (AF3)."""

    def __init__(self, director: InterviewDirector, **kwargs):
        super().__init__(**kwargs)
        self.director = director

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import LLMContextFrame
        from pipecat.processors.frame_processor import FrameDirection

        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.UPSTREAM:
            if await self.director.allow_rerun(frame):
                await self.push_frame(frame, direction)
            return
        await self.push_frame(frame, direction)
