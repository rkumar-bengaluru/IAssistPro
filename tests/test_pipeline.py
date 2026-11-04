import pytest

from iassistpro.contract import Limits
from iassistpro.limits import CLOSE_BUDGET, CLOSE_ENDED, CLOSE_TIME, LimitTracker
from iassistpro.store import SessionStore
from tests.conftest import CANARY
from tests.pipeline_harness import ToolLoopLLM, run_scripted


@pytest.fixture
def tmp_store(tmp_path):
    return SessionStore(tmp_path / "sessions", 24)


@pytest.fixture
def brief_3q(example_brief):
    return example_brief.model_copy(update={"questions": example_brief.questions[:3],
                                            "limits": Limits(max_follow_ups=1, max_minutes=10)})


def labels(store, hooks):
    return [(t.speaker, t.kind, t.question_id) for t in store.result(hooks.session_id).turns]


async def test_scripted_session_produces_turns_ids_and_end_reason(tmp_store, brief_3q):
    hooks = await run_scripted(brief_3q, tmp_store, answers=["a1", "a2", "a2 more", "a3"])
    assert labels(tmp_store, hooks) == [
        ("interviewer", "main", "q1"), ("candidate", "main", "q1"), ("interviewer", "main", "q2"),
        ("candidate", "main", "q2"), ("interviewer", "follow_up", "q2"), ("candidate", "follow_up", "q2"),
        ("interviewer", "main", "q3"), ("candidate", "main", "q3"), ("interviewer", "closing", None)]
    assert hooks.end_reason == "completed" and hooks.error is None
    r = tmp_store.result(hooks.session_id)
    assert [t.text for t in r.turns if t.speaker == "candidate"] == ["a1", "a2", "a2 more", "a3"]
    assert r.turns[0].text.startswith("Hello Sam, thanks for joining.") and r.latency.n == 4
    assert hooks.progress[0] == ("q1", "main", 1) and hooks.audio.state == "none"


async def test_usage_is_persisted_after_every_llm_turn(tmp_store, brief_3q, monkeypatch):
    calls = []
    orig = tmp_store.write_usage

    def spy(sid, usage):
        calls.append([u.model_copy() for u in usage])
        orig(sid, usage)

    monkeypatch.setattr(tmp_store, "write_usage", spy)
    hooks = await run_scripted(brief_3q, tmp_store, answers=["a1", "a2", "a2 more", "a3"])
    per_turn = [c for c in calls if c]
    assert hooks.llm.runs == 5 and len({len(c) and c[0].input_tokens for c in per_turn}) >= 5
    final = tmp_store.result(hooks.session_id).usage
    assert len(final) == 1 and final[0].model == "fake-interviewer"
    assert (final[0].input_tokens, final[0].cached_input_tokens, final[0].cache_write_tokens, final[0].output_tokens) == (
        1000, 4000, 1000, 200)


async def test_scripted_session_tool_first_still_speaks(tmp_store, brief_3q):
    hooks = await run_scripted(brief_3q, tmp_store, answers=["a1", "a2", "a2 more", "a3"], tool_first_no_text=True)
    turns = tmp_store.result(hooks.session_id).turns
    assert turns[0].speaker == "interviewer" and turns[0].text.startswith("Hello Sam")
    assert (turns[0].kind, turns[0].question_id) == ("main", "q1")
    assert hooks.llm.runs == 6 and hooks.end_reason == "completed"   # exactly one extra LLM run


async def test_budget_cap_ends_with_fixed_closing_and_no_extra_llm_call(tmp_store, brief_3q):
    tiny = brief_3q.model_copy(update={"limits": brief_3q.limits.model_copy(update={"max_llm_cost_usd": 0.0001})})
    hooks = await run_scripted(tiny, tmp_store, answers=["a1", "a2", "a3"])
    r = tmp_store.result(hooks.session_id)
    last_bot = [t for t in r.turns if t.speaker == "interviewer"][-1]   # the candidate may still say "thanks"
    assert hooks.end_reason == "budget" and last_bot.text == CLOSE_BUDGET.format(name="Sam")
    assert (last_bot.kind, last_bot.question_id) == ("closing", None)
    assert hooks.llm.runs <= 2


async def test_end_request_mid_session(tmp_store, brief_3q):
    hooks = await run_scripted(brief_3q, tmp_store, answers=["a1", "a2", "a3"], end_after=1)
    r = tmp_store.result(hooks.session_id)
    assert hooks.end_reason == "ended_by_candidate"
    assert r.turns[-1].text == CLOSE_ENDED.format(name="Sam") and r.turns[-1].kind == "closing"
    assert [t.text for t in r.turns if t.speaker == "candidate"] == ["a1"]


async def test_disconnect_ends_disconnected_with_partial_transcript(tmp_store, brief_3q):
    hooks = await run_scripted(brief_3q, tmp_store, answers=["a1", "a2", "a3"], disconnect_after=1)
    r = tmp_store.result(hooks.session_id)
    assert hooks.end_reason == "disconnected"
    assert [t.text for t in r.turns if t.speaker == "candidate"] == ["a1"] and r.usage and r.llm_cost_usd > 0


async def test_full_session_logs_no_content(tmp_store, captured_logs, example_brief):
    data = example_brief.model_dump()
    data["role"]["summary"] += f" {CANARY}"
    data["candidate"]["summary"] += f" {CANARY}"
    data["questions"] = data["questions"][:3]
    for q in data["questions"]:
        q["text"] += f" {CANARY}"
        q["probe_for"] = [f"probe {CANARY}"]
    brief = type(example_brief).model_validate(data)
    await run_scripted(brief, tmp_store, answers=[f"my answer {CANARY}"] * 4)
    out = captured_logs()
    assert CANARY not in out and "turn_closed" in out and "session_end" in out


async def test_scripted_session_with_a_mid_answer_pause_runs_the_llm_once_per_answer(tmp_store, brief_3q):
    # The second answer ends early at a sentence pause; the candidate resumes while the reply has just started.
    hooks = await run_scripted(brief_3q, tmp_store, answers=["a1", ("a2 first.", "a2 rest."), "a2 more", "a3"],
                               tts_speak_s=0.6)
    r = tmp_store.result(hooks.session_id)
    assert labels(tmp_store, hooks) == [
        ("interviewer", "main", "q1"), ("candidate", "main", "q1"), ("interviewer", "main", "q2"),
        ("candidate", "main", "q2"), ("interviewer", "follow_up", "q2"), ("candidate", "follow_up", "q2"),
        ("interviewer", "main", "q3"), ("candidate", "main", "q3"), ("interviewer", "closing", None)]
    assert [t.text for t in r.turns if t.speaker == "candidate"] == ["a1", "a2 first. a2 rest.", "a2 more", "a3"]
    # answers + greeting, plus the one run the early turn end had already started before the resume
    assert hooks.llm.runs == 4 + 1 + 1 and hooks.end_reason == "completed"


async def test_resumes_are_counted_without_content(tmp_store, brief_3q, captured_logs):
    hooks = await run_scripted(brief_3q, tmp_store, answers=["a1", ("a2 first.", "a2 rest."), "a2 more", "a3"],
                               tts_speak_s=0.6)
    r = tmp_store.result(hooks.session_id)
    assert r.latency.resumes == 1
    logs = captured_logs()
    assert "turn_resumed gap_ms=" in logs and "a2 first" not in logs and "a2 rest" not in logs


# ---- AF3: a tool-only re-run goes through the director like any other LLM run

def last_bot(store, hooks):
    return [t for t in store.result(hooks.session_id).turns if t.speaker == "interviewer"][-1]


async def test_tool_only_rerun_is_refused_past_the_cap(tmp_store, brief_3q):
    tiny = brief_3q.model_copy(update={"limits": brief_3q.limits.model_copy(update={"max_llm_cost_usd": 0.0001})})
    llm = ToolLoopLLM(tiny)
    hooks = await run_scripted(tiny, tmp_store, answers=[], llm=llm, timeout=10)
    r = tmp_store.result(hooks.session_id)
    assert hooks.end_reason == "budget" and last_bot(tmp_store, hooks).text == CLOSE_BUDGET.format(name="Sam")
    assert llm.runs == 1 and r.llm_cost_usd <= 0.0001


async def test_tool_only_rerun_respects_the_time_hard_stop(tmp_store, brief_3q, monkeypatch):
    llm = ToolLoopLLM(brief_3q)
    monkeypatch.setattr(LimitTracker, "time_state", lambda self, el: "hard_stop" if llm.runs >= 1 else "ok")
    hooks = await run_scripted(brief_3q, tmp_store, answers=[], llm=llm, timeout=10)
    assert hooks.end_reason == "time_limit" and last_bot(tmp_store, hooks).text == CLOSE_TIME.format(name="Sam")
    assert llm.runs == 1


async def test_two_silent_reruns_force_the_closing(tmp_store, brief_3q, captured_logs):
    llm = ToolLoopLLM(brief_3q)
    hooks = await run_scripted(brief_3q, tmp_store, answers=[], llm=llm, timeout=10)
    r = tmp_store.result(hooks.session_id)
    assert (hooks.end_reason, hooks.error) == ("error", "no_speech_loop")
    bot = last_bot(tmp_store, hooks)
    assert bot.text == CLOSE_BUDGET.format(name="Sam") and (bot.kind, bot.question_id) == ("closing", None)
    assert llm.runs == 3 and r.llm_cost_usd <= brief_3q.limits.max_llm_cost_usd   # the first run + 2 re-runs
    assert "llm_runs=3" in captured_logs()
