"""Cost-weighted cap, time and question limits (pure Python; no Pipecat import)."""

from typing import Literal

from .contract import Limits, Prices, Usage, llm_cost_usd

WRAP_UP_GRACE_S = 90

NOTE_PREFIX = "[Interviewer note, not said by the candidate] "
NOTE_NO_MORE_FOLLOW_UPS = "No more follow-ups on {qid}; move on to the next main question."
NOTE_LAST_MAIN = (
    "That was the last main question. After this answer and any follow-ups, close the interview in one sentence "
    "and call end_interview with reason completed."
)
NOTE_TIME_UP = (
    "Time is up. Thank the candidate and close the interview in one sentence now, then call end_interview with "
    "reason time_limit."
)
NOTE_BUDGET = "Close the interview in one sentence now, then call end_interview with reason budget."

CLOSE_TIME = "We're out of time, so let's stop here. Thank you, {name}, that's the end of the interview."
CLOSE_BUDGET = "Let's stop here. Thank you, {name}, that's the end of the interview."
CLOSE_ENDED = "Thanks, {name}. We'll stop here."

_COUNTS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")


class CostMeter:
    def __init__(self, prices: Prices, cap_usd: float):
        self.prices = prices
        self.cap_usd = cap_usd
        self.spent_usd = 0.0
        self.last_turn_usd = 0.0
        self._rows: dict[tuple[str, str, str], Usage] = {}

    def add(self, u: Usage) -> float:
        cost = llm_cost_usd([u], self.prices)
        key = (u.kind, u.provider, u.model)
        row = self._rows.get(key)
        if row is None:
            self._rows[key] = u.model_copy()
        else:
            upd = {c: getattr(row, c) + getattr(u, c) for c in _COUNTS}
            if u.seconds is not None:
                upd["seconds"] = (row.seconds or 0) + u.seconds
            if u.characters is not None:
                upd["characters"] = (row.characters or 0) + u.characters
            self._rows[key] = row.model_copy(update=upd)
        if u.kind == "llm":
            self.spent_usd += cost
            self.last_turn_usd = cost
        return cost

    def rows(self) -> list[Usage]:
        return list(self._rows.values())

    def near_cap(self) -> bool:
        return self.spent_usd >= 0.8 * self.cap_usd

    def next_turn_would_exceed(self) -> bool:
        return self.spent_usd + 1.25 * self.last_turn_usd > self.cap_usd


class LimitTracker:
    def __init__(self, limits: Limits, planned_ids: list[str]):
        self.limits = limits
        self.planned_ids = list(planned_ids)
        self._asked: list[str] = []
        self._follow_ups: dict[str, int] = {}

    def on_ask(self, qid: str) -> None:
        if qid not in self._asked:
            self._asked.append(qid)

    def on_follow_up(self, qid: str) -> None:
        self._follow_ups[qid] = self._follow_ups.get(qid, 0) + 1

    def follow_ups(self, qid: str) -> int:
        return self._follow_ups.get(qid, 0)

    @property
    def main_asked(self) -> int:
        return len(self._asked)

    def follow_up_limit_reached(self, qid: str) -> bool:
        return self.follow_ups(qid) >= self.limits.max_follow_ups

    def last_main_asked(self) -> bool:
        return all(q in self._asked for q in self.planned_ids)

    def time_state(self, elapsed_s: float) -> Literal["ok", "wrap_up", "hard_stop"]:
        limit = self.limits.max_minutes * 60
        if elapsed_s >= limit + WRAP_UP_GRACE_S:
            return "hard_stop"
        if elapsed_s >= limit:
            return "wrap_up"
        return "ok"
