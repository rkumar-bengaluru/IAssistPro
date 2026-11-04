"""Turn-taking helpers around Pipecat's user-turn strategies (rulings AT1, AT2).

GraceUserTurnStopStrategy wraps a stop strategy (smart-turn by default) and holds its "the turn has ended" decision
until the candidate has been silent for `grace_s` since their last speech. VAD still stops after 0.2 s, so Whisper
starts at once and transcribes inside the grace; speech during the grace drops the pending end and the turn goes on.
Silence is counted from the audio frames themselves (not wall time), so it is exact and testable.
"""

from pipecat.frames.frames import (
    Frame, InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_stop.base_user_turn_stop_strategy import BaseUserTurnStopStrategy


class GraceUserTurnStopStrategy(BaseUserTurnStopStrategy):
    def __init__(self, inner: BaseUserTurnStopStrategy, *, grace_s: float, **kwargs):
        super().__init__(**kwargs)
        self._inner = inner
        self._grace_ms = max(0.0, grace_s) * 1000
        self._speaking = False
        self._silence_ms = 0.0
        self._pending = None  # the inner strategy's stop params, held until the grace has passed
        inner.add_event_handler("on_user_turn_stopped", self._on_inner_stopped)

    @property
    def inner(self) -> BaseUserTurnStopStrategy:
        return self._inner

    # ---- the wrapper forwards everything except the stop decision (as Pipecat's own deferred wrapper does)
    def add_event_handler(self, event_name: str, handler):
        if event_name == "on_user_turn_stopped":
            super().add_event_handler(event_name, handler)
        else:
            self._inner.add_event_handler(event_name, handler)

    @property
    def resolves_proposed_turn_stop_frames(self) -> bool:
        return self._pending is not None or self._inner.resolves_proposed_turn_stop_frames

    @property
    def waits_for_inference(self) -> bool:
        return self._inner.waits_for_inference

    @property
    def decision_scheduled(self) -> bool:
        return self._pending is not None or self._inner.decision_scheduled

    async def setup(self, setup):
        await super().setup(setup)
        await self._inner.setup(setup)

    async def cleanup(self):
        await super().cleanup()
        await self._inner.cleanup()

    async def handle_user_turn_started(self):
        self._pending = None
        await self._inner.handle_user_turn_started()

    async def handle_user_turn_stopped(self):
        self._pending = None
        await self._inner.handle_user_turn_stopped()

    async def process_frame(self, frame: Frame):
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking, self._pending = True, None
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speaking = False
            self._silence_ms = 1000 * (frame.stop_secs or 0.0)
        elif isinstance(frame, InputAudioRawFrame) and not self._speaking:
            channels = frame.num_channels or 1
            self._silence_ms += 1000 * len(frame.audio) / (2 * channels * frame.sample_rate)
        await self._inner.process_frame(frame)
        await self._release_if_due()

    # ---- the grace
    def _grace_passed(self) -> bool:
        return not self._speaking and self._silence_ms >= self._grace_ms - 1e-6

    async def _on_inner_stopped(self, _inner, params) -> None:
        self._pending = params
        await self._release_if_due()

    async def _release_if_due(self) -> None:
        if self._pending is None or not self._grace_passed():
            return
        params, self._pending = self._pending, None
        await self.report_user_turn_stopped(enable_user_speaking_frames=params.enable_user_speaking_frames,
                                            confirms_speculation=params.confirms_speculation)


class ResumeTracker:
    """Decides whether the candidate starting to speak is a resume of the turn that just ended (AT2).

    A resume needs all three: a reply was started for that turn, it has not been heard in full yet, and the candidate
    starts within `window_s` of the turn end. Times are seconds on any one clock.
    """

    def __init__(self, window_s: float):
        self.window_s = window_s
        self._stopped_at: float | None = None
        self._reply_started = False
        self._reply_finished = False

    def on_user_stopped(self, t: float) -> None:
        self._stopped_at, self._reply_started, self._reply_finished = t, False, False

    def on_reply_started(self) -> None:
        if self._stopped_at is not None:
            self._reply_started = True

    def on_reply_finished(self) -> None:
        if self._reply_started:
            self._reply_finished = True

    def on_user_started(self, t: float) -> int | None:
        """The gap in ms if this start is a resume, else None. Either way the ended turn is settled."""
        stopped, started, finished = self._stopped_at, self._reply_started, self._reply_finished
        self._stopped_at, self._reply_started, self._reply_finished = None, False, False
        if stopped is None or not started or finished or self.window_s <= 0:
            return None
        gap = t - stopped
        return int(round(1000 * gap)) if 0 <= gap <= self.window_s else None


def _is_note(block) -> bool:
    from .limits import NOTE_PREFIX

    return isinstance(block, dict) and str(block.get("text", "")).startswith(NOTE_PREFIX)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and not _is_note(b)).strip()
    return ""


def merge_resumed_turn(context) -> bool:
    """Fold the last user message into the previous one and drop the cancelled reply between them.

    Returns False (context untouched) unless the context ends user, (assistant/tool messages), user.
    """
    msgs = list(context.get_messages())
    if not msgs or not isinstance(msgs[-1], dict) or msgs[-1].get("role") != "user":
        return False
    prev = next((i for i in range(len(msgs) - 2, -1, -1)
                 if isinstance(msgs[i], dict) and msgs[i].get("role") == "user"), None)
    if prev is None:
        return False
    if any(not isinstance(m, dict) or m.get("role") not in ("assistant", "tool") for m in msgs[prev + 1:-1]):
        return False
    added = _text_of(msgs[-1].get("content"))
    first = dict(msgs[prev])
    content = first.get("content")
    if isinstance(content, str):
        first["content"] = f"{content} {added}".strip()
    elif isinstance(content, list):
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        idx = max((i for i, b in enumerate(blocks)
                   if isinstance(b, dict) and b.get("type") == "text" and not _is_note(b)), default=None)
        if idx is None:
            blocks.insert(0, {"type": "text", "text": added})
        else:
            blocks[idx]["text"] = f"{blocks[idx]['text']} {added}".strip()
        first["content"] = blocks
    else:
        return False
    context.set_messages(msgs[:prev] + [first])
    return True
