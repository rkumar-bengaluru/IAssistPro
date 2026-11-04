"""Transcript turns with question labels, and server-side reply latency.

TranscriptBuilder.on_frame is pure (frame, t_ms since the client connected); TranscriptObserver feeds it from the
pipeline. Labels come from the director's tool handling, as TurnLabelFrames pushed by the LLM in stream order
(so a label lands on the reply it belongs to), or directly through set_label.
"""

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, DataFrame, InterruptionFrame, LLMFullResponseEndFrame,
    LLMFullResponseStartFrame, TranscriptionFrame, TTSSpeakFrame, TTSTextFrame, UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

from .contract import Latency, Turn

MAX_GAP_MS = 12_000


@dataclass
class TurnLabelFrame(DataFrame):
    """Carries the interviewer's current question label in stream order (never spoken, never in context)."""

    question_id: str | None = None
    kind: str = "greeting"


@dataclass
class TurnResumedFrame(DataFrame):
    """The director's decision that the candidate resumed the answer whose turn just ended (AT2)."""

    gap_ms: int = 0


@dataclass
class _BotTurn:
    texts: list[str] = field(default_factory=list)
    start_ms: int | None = None
    end_ms: int | None = None
    ended: bool = False
    frozen: bool = False


def _join(texts: list[str]) -> str:
    return " ".join(" ".join(t.split()) for t in texts if t and t.strip()).strip()


def p90(xs: list[int]) -> int:
    s = sorted(xs)
    return s[max(0, round(0.9 * len(s)) - 1)]


class TranscriptBuilder:
    def __init__(self, on_turn: Callable[[Turn], None], vad_stop_s: float = 0.2,
                 on_retract: Callable[[int], None] | None = None):
        self.on_turn = on_turn
        self.on_retract = on_retract or (lambda n: None)
        self.resumes = 0
        self._last_cand_idx: int | None = None
        self._gap_from_last_cand = False
        self._resume_into: Turn | None = None
        self.vad_stop_s = vad_stop_s
        self.turns: list[Turn] = []
        self.reply_gaps_ms: list[int] = []
        self._label: tuple[str | None, str] = (None, "greeting")
        self._bot: _BotTurn | None = None
        self._muted = False
        self._cand_open = False
        self._cand_start: int | None = None
        self._cand_label: tuple[str | None, str] = self._label
        self._cand_texts: list[str] = []
        self._pending_vad_start: int | None = None
        self._last_vad_end: int | None = None
        self._last_cand_end: int | None = None

    # ---- labels
    def set_label(self, question_id: str | None, kind: str) -> None:
        self._label = (question_id, kind)

    # ---- frames
    def on_frame(self, frame, t_ms: int) -> None:
        if isinstance(frame, TurnLabelFrame):
            self.set_label(frame.question_id, frame.kind)
        elif isinstance(frame, TurnResumedFrame):
            self._resume()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._start_reply(ended=False)
        elif isinstance(frame, TTSSpeakFrame):
            self._start_reply(ended=True)
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._bot is not None and not self._muted:
                self._bot.ended = True
                # A reply whose audio already stopped closes when the candidate speaks, the next reply starts,
                # or at flush(); the bot may only be pausing between sentences.
        elif isinstance(frame, TTSTextFrame):
            if self._muted:
                return
            if self._bot is None:
                self._bot = _BotTurn()
            if not self._bot.frozen:
                self._bot.texts.append(frame.text)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking_now = True
            if self._last_cand_end is not None:
                gap = t_ms - self._last_cand_end
                if 0 <= gap < MAX_GAP_MS:
                    self.reply_gaps_ms.append(gap)
                    self._gap_from_last_cand = True
                self._last_cand_end = None
            if self._bot is not None and self._bot.start_ms is None:
                self._bot.start_ms = t_ms
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking_now = False
            if self._bot is not None and self._bot.start_ms is not None:
                self._bot.end_ms = t_ms
                if self._bot.ended:
                    self._close_bot()
        elif isinstance(frame, InterruptionFrame):
            if self._bot is not None:
                if self._bot.start_ms is None:
                    self._bot = None  # nothing of it was heard
                else:
                    self._bot.frozen = True
                    self._bot.ended = True
                    if not self._bot_speaking_now:
                        self._close_bot()
            self._muted = True
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if not self._cand_open and self._pending_vad_start is None:
                self._pending_vad_start = max(0, t_ms - int(round(1000 * (frame.start_secs or 0))))
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._close_finished_bot()
            if not self._cand_open:
                self._cand_open = True
                self._cand_start = self._pending_vad_start if self._pending_vad_start is not None else t_ms
                self._cand_label = self._label
                self._cand_texts = []
                self._last_vad_end = None
                if self._resume_into is not None:
                    self._reopen(self._resume_into)
            self._pending_vad_start = None
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            stop = frame.stop_secs or self.vad_stop_s
            self._last_vad_end = max(0, t_ms - int(round(1000 * stop)))
        elif isinstance(frame, TranscriptionFrame):
            if self._cand_open:
                self._cand_texts.append(frame.text)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if self._cand_open:
                self._close_candidate(t_ms)

    _bot_speaking_now = False

    def _start_reply(self, *, ended: bool) -> None:
        self._muted = False
        if self._bot is not None:
            if self._bot.start_ms is None and not self._bot.texts:
                self._bot.ended = ended  # e.g. the re-run after a tool call that spoke nothing
                return
            if self._bot.start_ms is not None:
                self._bot.ended = True
                if self._bot.end_ms is None:
                    self._bot.end_ms = self._bot.start_ms
                self._close_bot()
            else:
                self._bot = None
        self._bot = _BotTurn(ended=ended)

    def _close_finished_bot(self) -> None:
        b = self._bot
        if b is not None and b.ended and b.start_ms is not None and b.end_ms is not None and not self._bot_speaking_now:
            self._close_bot()

    def flush(self, t_ms: int) -> None:
        """Close whatever is open at the end of the session (spoken words only)."""
        if self._bot is not None and self._bot.start_ms is not None:
            if self._bot.end_ms is None or self._bot_speaking_now:
                self._bot.end_ms = t_ms
            self._close_bot()
        self._bot = None
        if self._cand_open:
            self._close_candidate(t_ms)

    def _close_bot(self) -> None:
        b, self._bot = self._bot, None
        text = _join(b.texts)
        if not text or b.start_ms is None:
            return
        qid, kind = self._label
        self._emit(Turn(speaker="interviewer", text=text, start_ms=b.start_ms, end_ms=b.end_ms or b.start_ms,
                        question_id=qid, kind=kind))

    def _close_candidate(self, t_ms: int) -> None:
        self._cand_open = False
        start = self._cand_start if self._cand_start is not None else t_ms
        end = self._last_vad_end if self._last_vad_end is not None and self._last_vad_end >= start else t_ms
        text = _join(self._cand_texts)
        self._cand_texts = []
        self._last_vad_end = None
        if not text:
            return
        qid, kind = self._cand_label
        self._last_cand_end = end
        self._gap_from_last_cand = False
        self._emit(Turn(speaker="candidate", text=text, start_ms=start, end_ms=end, question_id=qid, kind=kind))
        self._last_cand_idx = len(self.turns) - 1

    # ---- resume merge (AT2)
    def _resume(self) -> None:
        """Take back the ended fragment and the cancelled reply; the open candidate turn continues the fragment."""
        self.resumes += 1
        i = self._last_cand_idx
        if i is None or i >= len(self.turns) or self.turns[i].speaker != "candidate":
            return
        removed = self.turns[i:]
        self.turns = self.turns[:i]
        self._last_cand_idx = None
        self._bot = None  # the cancelled reply, if it hasn't closed yet, never becomes a turn
        if self._gap_from_last_cand and self.reply_gaps_ms:
            self.reply_gaps_ms.pop()
        self._gap_from_last_cand = False
        self._last_cand_end = None
        self.on_retract(len(removed))
        if self._cand_open:
            self._reopen(removed[0])
        else:
            self._resume_into = removed[0]

    def _reopen(self, fragment: Turn) -> None:
        self._resume_into = None
        self._cand_start = fragment.start_ms
        self._cand_texts = [fragment.text] + self._cand_texts
        self._cand_label = (fragment.question_id, fragment.kind)

    def _emit(self, turn: Turn) -> None:
        self.turns.append(turn)
        self.on_turn(turn)

    def latency(self) -> Latency:
        g = self.reply_gaps_ms
        if not g:
            return Latency(n=0, resumes=self.resumes)
        return Latency(n=len(g), median_reply_ms=int(round(statistics.median(g))), p90_reply_ms=p90(g),
                       resumes=self.resumes)


_WATCHED = (
    TurnLabelFrame, TurnResumedFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame, TTSSpeakFrame, TTSTextFrame,
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame, VADUserStartedSpeakingFrame,
    UserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, TranscriptionFrame, UserStoppedSpeakingFrame,
)


class TranscriptObserver(BaseObserver):
    """Feeds the builder each watched frame once (its first push), timed from the client connect."""

    def __init__(self, builder: TranscriptBuilder, **kwargs):
        super().__init__(**kwargs)
        self.builder = builder
        self._t0_mono: float | None = None
        self._t0_wall: float | None = None
        self._seen: set[int] = set()

    def start(self) -> None:
        self._t0_mono, self._t0_wall = time.monotonic(), time.time()

    def elapsed_s(self) -> float:
        return 0.0 if self._t0_mono is None else time.monotonic() - self._t0_mono

    async def on_push_frame(self, data: FramePushed):
        f = data.frame
        if self._t0_mono is None or not isinstance(f, _WATCHED) or f.id in self._seen:
            return
        self._seen.add(f.id)
        if isinstance(f, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            t_ms = int(round(1000 * (f.timestamp - self._t0_wall)))
        else:
            t_ms = int(round(1000 * (time.monotonic() - self._t0_mono)))
        self.builder.on_frame(f, max(0, t_ms))
