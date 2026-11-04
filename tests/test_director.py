import pytest

from iassistpro.contract import Limits, Prices, Usage
from iassistpro.director import InterviewState
from iassistpro.limits import NOTE_LAST_MAIN, NOTE_NO_MORE_FOLLOW_UPS, NOTE_PREFIX, NOTE_TIME_UP, CostMeter, LimitTracker

HAIKU = Prices(input=0.10, cached_input=0.01, cache_write=0.125, output=0.50)


@pytest.fixture
def state(example_brief):
    brief = example_brief.model_copy(update={"limits": Limits(max_follow_ups=2, max_minutes=25)})
    return InterviewState(brief, CostMeter(HAIKU, brief.limits.max_llm_cost_usd),
                          LimitTracker(brief.limits, [q.id for q in brief.planned_questions]))


def test_initial_state_is_greeting(state):
    assert (state.current_question_id, state.current_kind, state.question_number, state.ending) == (None, "greeting", 0, False)


def test_tool_call_with_text_does_not_rerun_llm(state):
    assert state.on_tool("ask_question", {"question_id": "q1"}, response_had_text=True).run_llm is False
    assert (state.current_question_id, state.current_kind, state.question_number) == ("q1", "main", 1)


def test_tool_call_without_text_triggers_one_llm_run(state):
    assert state.on_tool("ask_question", {"question_id": "q1"}, response_had_text=False).run_llm is True


def test_follow_up_counts_and_note_once(state):
    state.on_tool("ask_question", {"question_id": "q2"}, True)
    state.on_tool("follow_up", {"question_id": "q2"}, True)
    assert state.current_kind == "follow_up" and state.notes_due(10) == []
    state.on_tool("follow_up", {"question_id": "q2"}, True)
    note = NOTE_PREFIX + NOTE_NO_MORE_FOLLOW_UPS.format(qid="q2")
    assert state.notes_due(20) == [note]
    assert state.notes_due(30) == []


def test_last_main_note(state):
    for q in ["q1", "q2", "q3", "q4", "q5"]:
        state.on_tool("ask_question", {"question_id": q}, True)
    assert state.notes_due(10) == []
    state.on_tool("ask_question", {"question_id": "q6"}, True)
    assert state.notes_due(10) == [NOTE_PREFIX + NOTE_LAST_MAIN] and state.notes_due(11) == []


def test_time_wrap_then_hard_stop(state):
    assert state.notes_due(1499) == []
    assert state.notes_due(1500) == [NOTE_PREFIX + NOTE_TIME_UP]
    assert state.hard_stop_due(1589) is None and state.hard_stop_due(1590) == "time_limit"


def test_budget_hard_stop_before_next_run(state):
    assert state.hard_stop_due(10) is None
    state.meter.add(Usage(provider="anthropic", model="claude-haiku-5-5", input_tokens=2_000_000, output_tokens=400_000))
    assert state.hard_stop_due(10) == "budget"


def test_budget_note_near_cap(state):
    state.meter.add(Usage(provider="anthropic", model="claude-haiku-5-5", input_tokens=1_000, output_tokens=1_000))
    state.meter.add(Usage(provider="anthropic", model="claude-haiku-5-5", input_tokens=4_000_000))   # 0.40 + small
    notes = state.notes_due(10)
    assert any("call end_interview with reason budget" in n for n in notes)


def _apply(state, ev):
    if ev.startswith("end:"):
        state.on_tool("end_interview", {"reason": ev[4:]}, True)
    elif ev.startswith("hard:"):
        state.mark_hard_stop(ev[5:])
    elif ev == "time_wrap":
        state.notes_due(1500)
    elif ev == "request_end":
        state.request_end()
    elif ev == "disconnect":
        state.on_disconnect()
    elif ev == "error":
        state.on_error()


@pytest.mark.parametrize("events, expected", [
    (["end:completed"], "completed"), (["end:candidate_request"], "ended_by_candidate"),
    (["time_wrap", "end:completed"], "time_limit"), (["request_end", "end:completed"], "ended_by_candidate"),
    (["disconnect"], "disconnected"), (["end:completed", "disconnect"], "completed"), (["error"], "error"),
    (["hard:budget", "disconnect"], "budget")])
def test_end_reason_precedence(state, events, expected):
    for ev in events:
        _apply(state, ev)
    assert state.final_end_reason() == expected


def test_end_interview_sets_closing(state):
    state.on_tool("ask_question", {"question_id": "q1"}, True)
    state.on_tool("end_interview", {"reason": "completed"}, True)
    assert state.ending and (state.current_question_id, state.current_kind) == (None, "closing")


def test_unknown_question_id_keeps_current(state):
    state.on_tool("ask_question", {"question_id": "q1"}, True)
    out = state.on_tool("ask_question", {"question_id": "q99"}, True)
    assert out.result == {"ok": False, "error": "unknown_question_id"}
    assert (state.current_question_id, state.question_number) == ("q1", 1)


async def test_note_block_survives_anthropic_adapter():
    """G9/APR3: a note appended as an extra text block on the last user message reaches Anthropic intact."""
    from pipecat.adapters.llm.services.anthropic_adapter import AnthropicLLMAdapter
    from pipecat.processors.aggregators.llm_context import LLMContext

    from iassistpro.director import append_notes

    ctx = LLMContext(messages=[{"role": "user", "content": "(joined)"}, {"role": "assistant", "content": "Hi."},
                               {"role": "user", "content": "my answer"}])
    append_notes(ctx, [NOTE_PREFIX + "note one"])
    params = await AnthropicLLMAdapter().get_llm_invocation_params(ctx, enable_prompt_caching=True, system_instruction="s")
    last = params["messages"][-1]
    assert last["role"] == "user"
    assert [b["text"] for b in last["content"]] == ["my answer", NOTE_PREFIX + "note one"]
    # Append-only: earlier messages are unchanged, so the cached prefix stays valid.
    first = params["messages"][0]["content"]
    assert (first if isinstance(first, str) else first[0]["text"]) == "(joined)"
