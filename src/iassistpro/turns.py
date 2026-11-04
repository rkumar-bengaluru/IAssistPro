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
