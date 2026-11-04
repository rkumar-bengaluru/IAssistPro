import pytest

from iassistpro.contract import Limits, Prices, Usage
from iassistpro.limits import (
    CLOSE_BUDGET, NOTE_LAST_MAIN, NOTE_NO_MORE_FOLLOW_UPS, NOTE_PREFIX, CostMeter, LimitTracker,
)

HAIKU = Prices(input=0.10, cached_input=0.01, cache_write=0.125, output=0.50)


def llm(model="claude-haiku-5-5", **kw) -> Usage:
    return Usage(provider="anthropic", model=model, **kw)


def test_meter_accumulates_and_projects():
    m = CostMeter(HAIKU, cap_usd=0.001)
    assert m.add(llm(input_tokens=2000, output_tokens=200)) == pytest.approx(0.0003)
    assert m.spent_usd == pytest.approx(0.0003) and not m.near_cap() and not m.next_turn_would_exceed()
    m.add(llm(input_tokens=2000, output_tokens=200))
    m.add(llm(input_tokens=2000, output_tokens=200))
    assert m.spent_usd == pytest.approx(0.0009) and m.last_turn_usd == pytest.approx(0.0003)
    assert m.near_cap() and m.next_turn_would_exceed()


def test_cached_reads_are_cheap_so_a_long_session_fits():
    for cap in (0.50, 0.05):
        m = CostMeter(HAIKU, cap_usd=cap)
        for _ in range(40):
            m.add(llm(input_tokens=200, cached_input_tokens=6000, output_tokens=60))
        assert m.spent_usd < cap and not m.next_turn_would_exceed()


def test_rows_sum_per_model():
    m = CostMeter(HAIKU, cap_usd=1)
    m.add(llm(input_tokens=1, cached_input_tokens=2, cache_write_tokens=3, output_tokens=4))
    m.add(llm(input_tokens=10, cached_input_tokens=20, cache_write_tokens=30, output_tokens=40))
    m.add(llm(model="claude-sonnet-5-5", input_tokens=5))
    rows = {r.model: r for r in m.rows()}
    assert set(rows) == {"claude-haiku-5-5", "claude-sonnet-5-5"}
    h = rows["claude-haiku-5-5"]
    assert (h.input_tokens, h.cached_input_tokens, h.cache_write_tokens, h.output_tokens) == (11, 22, 33, 44)


def test_time_states():
    t = LimitTracker(Limits(max_minutes=25), ["q1"])
    assert [t.time_state(x) for x in (1499, 1500, 1589, 1590)] == ["ok", "wrap_up", "wrap_up", "hard_stop"]


def test_follow_up_limit_and_last_main():
    t = LimitTracker(Limits(max_follow_ups=2), ["q1", "q2", "q3"])
    t.on_ask("q1")
    t.on_ask("q2")
    t.on_follow_up("q2")
    assert not t.follow_up_limit_reached("q2") and t.follow_ups("q2") == 1
    t.on_follow_up("q2")
    assert t.follow_up_limit_reached("q2") and not t.follow_up_limit_reached("q1")
    assert t.main_asked == 2 and not t.last_main_asked()
    t.on_ask("q3")
    assert t.last_main_asked() and t.main_asked == 3


def test_note_texts():
    assert NOTE_PREFIX == "[Interviewer note, not said by the candidate] "
    assert NOTE_NO_MORE_FOLLOW_UPS.format(qid="q2") == "No more follow-ups on q2; move on to the next main question."
    assert "end_interview with reason completed" in NOTE_LAST_MAIN
    assert CLOSE_BUDGET.format(name="Sam") == "Let's stop here. Thank you, Sam, that's the end of the interview."
