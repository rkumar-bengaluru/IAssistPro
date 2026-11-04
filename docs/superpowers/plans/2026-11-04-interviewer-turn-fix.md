# Interviewer turn-taking fix (Release 2a follow-up)

> Execution: inline (superpowers:executing-plans), TDD, on `feat/mock-interviewer`. Ruling: `docs/rupak_decisions.md`, "2a acceptance ruling by the architect" (AT1–AT6).

**Goal:** the interviewer never starts talking while the candidate is only pausing between sentences, and a candidate who resumes right after the interviewer starts doesn't get a second LLM run or a split answer. This trades a fraction of a second for never talking over Rupak.

**Two mechanisms:**
1. **A grace before the turn ends (AT1).** Smart-turn's COMPLETE verdict only ends the turn once at least `TURN_GRACE_S` (default **1.5 s**) of continuous silence has passed since the candidate's last speech. VAD still stops at 0.2 s, so Whisper starts at once and its ~1.4–2.2 s runs *inside* the grace. Speech during the grace cancels the pending end, and the segments join one turn.
2. **Resume merge (AT2).** If the candidate starts speaking within `RESUME_WINDOW_S` (default **2.0 s**) of a turn end, the interviewer's reply is cancelled (as today) and the turns are rejoined:
   - its partial assistant message is removed from the LLM context;
   - the two user messages become one;
   - the transcript keeps one candidate turn (the first fragment's `start_ms`, the last fragment's `end_ms`) and no interviewer turn for the cancelled reply;
   - the director doesn't advance (no question, follow-up or label change);
   - one LLM run happens when the merged turn ends.

Global constraints from the 2a plan stand (no personal data, never touch JobLens prod, live spend capped, stage explicit paths).

---

### Task 1: Grace stop strategy

**Files:** `src/iassistpro/turns.py` (new), `src/iassistpro/config.py` (`turn_grace_s: float = 1.5` from `TURN_GRACE_S`, 0.0–5.0; `resume_window_s: float = 2.0` from `RESUME_WINDOW_S`, 0.0–5.0), `src/iassistpro/providers.py` (`make_turn_strategies` wraps smart-turn in the grace strategy when `TURN_STOP=smart`), `tests/test_turns.py`.

- [ ] **Step 1: Failing tests** (the clock is driven by test frames, not wall time):
  - `test_complete_verdict_waits_for_the_grace`: speech stops at t=0, smart-turn says COMPLETE at t=0.25, the transcript is final at t=1.0. The turn ends at **t=1.5**, not before.
  - `test_speech_during_grace_cancels_the_end`: COMPLETE at 0.25, speech restarts at 0.9. No turn end. Then speech stops at 5.0, COMPLETE at 5.25: the turn ends at **6.5**, and it is **one** turn holding both transcripts.
  - `test_whisper_slower_than_grace_ends_on_transcript`: COMPLETE at 0.25, transcript final at 2.2. The turn ends at **2.2** (the grace adds nothing when Whisper is slower).
  - `test_incomplete_verdict_still_uses_the_fallback`: INCOMPLETE, then silence. The turn ends at Pipecat's 3 s fallback, unchanged.
  - `test_grace_zero_is_todays_behaviour`: `TURN_GRACE_S=0` ends at the transcript (t=1.0).
  - `test_settings_read_grace_and_window`: defaults 1.5 / 2.0; 5.1 is refused.
- [ ] **Step 2:** `./scripts/dtest.sh pytest -q tests/test_turns.py` → FAIL (no module).
- [ ] **Step 3: Implement** the grace strategy as a wrapper around Pipecat's stop strategy. Don't monkeypatch Pipecat.
- [ ] **Step 4:** the same command plus `tests/test_providers.py` → PASS.
- [ ] **Step 5: Commit** `feat: end a turn only after a silence grace, overlapping Whisper`.

### Task 2: Resume merge

**Files:** `src/iassistpro/turns.py`, `src/iassistpro/director.py`, `src/iassistpro/transcript.py`, `src/iassistpro/pipeline.py`, `tests/test_turns.py`, `tests/test_transcript.py`, `tests/test_pipeline.py`.

- [ ] **Step 1: Failing tests:**
  - `test_resume_within_window_merges_context`: user "A", assistant partial "Thanks, and", user "B" inside 2.0 s. The context sent to the LLM has **one** user message "A B" and no partial assistant message.
  - `test_resume_after_window_is_a_real_barge_in`: the same at 2.5 s. Today's behaviour (two user messages, the interrupted assistant message kept).
  - `test_merged_turn_is_one_transcript_turn`: one candidate turn spanning both fragments, and no interviewer turn for the cancelled reply.
  - `test_director_does_not_advance_on_a_cancelled_reply`: a `follow_up`/`ask_question` tool call from the cancelled reply is discarded: `question_number`, the follow-up count and the label are unchanged.
  - `test_scripted_session_with_a_mid_answer_pause_runs_the_llm_once_per_answer` (pipeline, fake LLM): a scripted answer with a 0.6 s sentence pause, and a forced early end-of-turn. **LLM runs = answers + 1 (greeting)**, and the transcript has 1 candidate turn per answer.
  - `test_resumes_are_counted_without_content`: `log_event("turn_resumed", gap_ms=…)` and the result's `latency` gains `resumes: int`; no text is logged.
- [ ] **Step 2:** `./scripts/dtest.sh pytest -q tests/test_turns.py tests/test_transcript.py tests/test_pipeline.py` → FAIL.
- [ ] **Step 3: Implement.** `latency.resumes` is an added optional field: update `contract.py`, regenerate the schemas, and update the example result (`resumes: 0`). Plan 2b ignores unknown fields (`extra="ignore"`), so there's no contract break; the decisions log notes it.
- [ ] **Step 4:** the full suite `./scripts/dtest.sh pytest -q` → PASS (139 + new).
- [ ] **Step 5: Commit** `feat: merge a candidate's resumed answer instead of a second LLM run`.

### Task 3: Free spoken e2e as the cut-off check (acceptance #2)

**Files:** `e2e/spoken.e2e.ts`, `e2e/lib/harness.ts`.

- [ ] **Step 1:** add assertions to the free test.
  - `splits == 0`: each scripted answer (from the `.schedule.json`) maps to exactly one candidate turn.
  - `talkOverMs == 0`: no bot audio energy while the fake mic is speaking, outside the first 300 ms after a scripted answer starts (measured in the page).
  - The LLM run count equals the main questions plus follow-ups plus the greeting.
- [ ] **Step 2: Run the free e2e 3 times with chromium and once with msedge.** Expected: **4/4 pass, 0 splits, 0 talk-over.** If any split remains, rerun all 4 once with `TURN_GRACE_S=2.0`. If that passes, 2.0 becomes the default and the change is logged; otherwise stop and report to the architect. No other tuning.
- [ ] **Step 3: Commit** `test: spoken e2e fails on a split answer or talk-over`.

### Task 4: Acceptance rerun under fixed conditions (AT4)

- [ ] **Step 1: Preconditions,** checked and written into `docs/latency-acceptance.md` before starting. If any is missing, stop and ask the coordinator.
  - The laptop is plugged in, with the Windows power mode on **Best performance**. Rupak sets this; the builder doesn't change system settings.
  - No other builder or agent session is running work. The coordinator pauses 2b.
  - JobLens **dev** containers are stopped by the coordinator. Prod is untouched.
  - Host free memory is ≥ 4 GB (Windows `Get-CimInstance Win32_OperatingSystem` FreePhysicalMemory).
  - Background host CPU is below 15% over 30 s.
- [ ] **Step 2:** change `scripts/latency_report.py`'s bounds to 4500/5500 ms and add a `splits` column (test-first: `test_report_bounds_are_4500_and_5500`). Then two complete live runs (Haiku, $0.05 brief cap each, total < $0.15). Both must pass **median ≤ 4500 ms and p90 ≤ 5500 ms** (browser, ≥ 10 gaps) with 0 splits. If one fails, one more run is allowed; 2 of 3 must pass, with every run reported.
- [ ] **Step 3:** update `docs/latency-acceptance.md` with both verdicts, and append the builder's calls to `docs/rupak_decisions.md` (append only). Commit only those two files, plus `scripts/latency_report.py` and its test: `docs: turn-taking fix acceptance`.
