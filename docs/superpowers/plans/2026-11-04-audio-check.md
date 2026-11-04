# Audio Check (IAssistPro side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Execution for this plan: inline via superpowers:executing-plans** (an architect agent approves for the owner).

**Goal:** Give the interviewer service a free, LLM-less **check session** (`POST /checks`) that tests audio both ways, stream the interviewer-side **mic level** and a **silent-input** alarm on checks and interviews alike, and add the pre-approved `BARGE_IN_MIN_S` fallback for no-headset use (UX5).

**Architecture:** A pure `LevelMeter` (new `levels.py`) turns inbound PCM into `mic_level` / `silent_input` messages; a thin `InputLevelMonitor` frame processor sits right after `transport.input()` in both pipelines and pushes them as `RTVIServerMessageFrame`s (Pipecat's RTVI observer forwards them on the data channel). A new `check.py` runs transport → monitor → STT → user aggregator (same turn rules) → `CheckController` → TTS → output, with no LLM; the controller speaks a fixed sentence and answers RTVI client messages (`listen`, `play_prompt`). `SessionManager` gains `CheckSession`s that share the one-session lock, the one-time token and the offer path with interviews, and store nothing.

**Tech Stack:** Python 3.12, Pipecat 1.15.0 (SmallWebRTC, RTVI), FastAPI, pytest (dev container), Playwright e2e with Chromium's fake microphone.

**Spec:** `docs/superpowers/specs/2026-11-04-interview-ux-and-audio-check-design.md` (§3.1, §3.3 service parts, §6 IAssistPro, §7 acceptance 1–3 service parts), rulings UX3, UX5, UX7 in `docs/rupak_decisions.md`. Current contract: "Service interface" in `docs/superpowers/plans/2026-11-04-interviewer-service.md` plus `docs/contract-changes.md`.

**Not in this plan (the JobLens plan):** the check screen, mic picker, virtual-device labels, Skip check, the live-screen level bar and "can't hear you" banner, `echoCancellation`/`noiseSuppression`/`autoGainControl` constraints, the offer proxy for `/checks`, setting `BARGE_IN_MIN_S` in JobLens's compose, and the live no-headset interview (acceptance 3's test).

## Global Constraints

- Repo `C:\2026\rkumar\IAssistPro`, branch `feat/audio-check` (checked out). Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. **Never push, merge or tag.** Stage explicit paths only; never stage `spike/`, `e2e/out/`, or the untracked `client_secret_*.json`.
- **No personal data (B2):** fixtures use only the invented candidate Sam; new audio is synthetic (Kokoro `am_michael`).
- **Keys:** never print, echo or log a key. Nothing in this plan needs the Anthropic key: every e2e here runs with `LLM_PROVIDER=fake`. **Live spend for this plan: $0.**
- **"An audio check that spends money" is an anti-goal:** the check constructs no LLM, records no usage and writes nothing under `SESSIONS_DIR`.
- **Logs:** INFO; only timings, sizes, counts, ids, codes. **Never transcript text** (the check's heard text included), SDP or audio. Use `log_event` (it redacts free text).
- **Never touch `joblens-*` containers, volumes or prod.** Only `iassistpro-*` containers; standalone ports HTTP `127.0.0.1:8770`, UDP `40020-40022`.
- Pins unchanged (`pipecat-ai[...]==1.15.0`, `aiortc==1.15.0`, `aioice==0.12.1`); no new dependencies.
- `CONTRACT_VERSION` stays **1**: every change here is additive. Each one gets a row in `docs/contract-changes.md`.
- Spec values (verbatim): prompt sentence **"This is your interviewer. If you can hear me, press 'I heard it'."**; mic level **~5/s**; silent input after **≥3 s** of **all-zero samples**; check ends after **2 minutes**; `BARGE_IN_MIN_S` **default 0** (= today), **0.8 s** for no-headset use; check target **under 30 s**, cost **$0**.
- Tests: `./scripts/dtest.sh pytest -q <files>` from Git Bash at the repo root. E2E: `cd e2e && npx playwright test <file>` against the standalone service started with `LLM_PROVIDER=fake docker compose -f docker/compose.yml up -d --build interviewer`.

## Review Focus

1. **The browser sends no audio packets at all** (muted track, Opus DTX, a device that delivers nothing): no frames reach the pipeline, so a frame-counting detector never fires. Expected: `silent_input` with `reason: "no_audio"` 3 s after connect (or after the last frame). Tests: `test_no_frames_for_3_s_is_silent_input` (Task 1), `test_interview_reports_no_audio_after_connect` (Task 2).
2. **A real but quiet room** (samples of ±1–2 LSB, a mic with a hardware noise floor). Expected: `mic_level` with a small non-zero `rms` and **never** `silent_input` (UX3: thinking quietly must not nag). Test: `test_quiet_room_noise_is_never_silent_input` (Task 1).
3. **"Begin interview" right after the check:** JobLens ends the check and immediately creates the session. Expected: `POST /checks/{id}/end` returns only once the lock is free, so `POST /sessions` gets 201, not 409 `busy`. Tests: `test_end_check_frees_the_lock_before_replying`, `test_end_check_cancels_a_stuck_check_after_the_wait` (Task 5).
4. **The interviewer hears itself through the laptop speakers** with `BARGE_IN_MIN_S=0.8`: a 0.5 s echo burst during bot speech, whose transcript arrives after the bot has stopped. Expected: no turn starts, the echo's words never enter the next turn. Test: `test_floor_drops_the_late_transcript_of_rejected_speech` (Task 3).
5. **A check abandoned in any way** (tab closed, never connected, left open past 2 minutes, mic switched by starting a new check). Expected: the lock is freed every time and a new check or session can start. Tests: `test_check_time_limit_ends_it`, `test_disconnect_ends_the_check` (Task 4), `test_unused_check_expires_and_frees_the_lock`, `test_disconnected_check_frees_the_lock` (Task 5).

---

## Contract additions (consumed by the JobLens plan)

All additive to contract version 1. Errors keep the shape `{"detail": {"code", "message", ...}}`.

**Endpoints**

| Method, path | Success | Errors |
|---|---|---|
| `POST /checks` (no body; an empty body or `{}` is accepted) | 201 `{"check_id": "<32 hex>", "connect": {"transport": "smallwebrtc", "offer_path": "/checks/{id}/offer", "token": "<one-time>", "expires_at": "<ISO 8601 UTC>"}}` | 409 `busy` (a session **or** check is waiting/live; no id); 503 `not_ready` (+`status`); 409 `wrong_state` when the service runs `TRANSPORT=websocket` (checks are SmallWebRTC only) |
| `POST /checks/{id}/offer` (SmallWebRTC SDP offer JSON; token in `X-Session-Token` or `?token=`) | 200 Pipecat answer `{sdp, type, pc_id}` | 401 `token_invalid`; 410 `token_expired`; 409 `wrong_state` (second offer, or check finished); 404 `unknown_check` |
| `PATCH /checks/{id}/offer` (`{pc_id, candidates}`, same token) | 200 `{"status": "success"}` | as above |
| `GET /checks/{id}` | 200 `CheckStatus` | 404 `unknown_check` |
| `POST /checks/{id}/end` | 202 `CheckStatus`, sent **only after the check has stopped and the lock is free** (waits up to 5 s, then cancels the check). Idempotent. | 404 `unknown_check` |

- Token rules are the sessions' (one offer within `CONNECT_TOKEN_TTL_S` = 120 s; afterwards PATCH only on that `pc_id`). JobLens sends the token server-side in `X-Session-Token`.
- A check id is not a session id: `/sessions/{check_id}` is 404 and vice versa. Finished checks are forgotten 10 minutes after they end (then 404).
- `CheckStatus`: `{"check_id": str, "state": "waiting" | "live" | "ended" | "failed", "elapsed_s": float, "end_reason": "ended_by_client" | "disconnected" | "time_limit" | "error" | null, "error": null | "not_connected" | "offer_failed" | "runner_error:<Type>" | "pipeline_error:<Type>"}`. Lifecycle: `waiting` → (offer) `live` → `ended`; `waiting` → `failed` (`not_connected`) after the token TTL; crashes → `failed`. A service restart forgets checks (nothing is stored).
- A check ends on `POST /checks/{id}/end`, on disconnect, or **120 s after the offer** (`time_limit`). It stores nothing, records no recording and no usage (cost $0), and never constructs an LLM.
- `GET /health`: unchanged fields; `busy` is also true while a check is waiting/live.
- New setting `BARGE_IN_MIN_S` (float, default `0`, range 0–3). `0` = today. When > 0, while the interviewer is speaking the candidate interrupts only after that many seconds of continuous speech; speech shorter than that is dropped with its transcript. Applies to interviews and checks. UX5's no-headset value is `0.8`.

**RTVI messages** (Pipecat RTVI on the WebRTC data channel; `@pipecat-ai/client-js` delivers server messages to `onServerMessage(data)`, client messages are sent with `client.sendClientMessage(type)`; the service sends no response to client messages).

Server → browser, wire form `{"label": "rtvi-ai", "type": "server-message", "data": <below>}`:

| `data` | When | Sessions | Checks |
|---|---|---|---|
| `{"type": "mic_level", "rms": float}` | every 200 ms of **received** audio (~5/s). `rms` = RMS of the 16 kHz int16 samples / 32768, range 0–1, 4 decimals. Exactly `0.0` for digital silence. No frames received → no messages. | yes | yes |
| `{"type": "silent_input", "reason": "zeros" \| "no_audio"}` | `zeros`: a continuous run of 3.0 s of frames whose samples are all exactly 0. `no_audio`: no inbound audio frame for 3.0 s since connect or since the last frame. Sent **once per silent episode**; re-armed by any frame with a non-zero sample. No "restored" message: the browser clears its warning when a `mic_level` with `rms > 0` arrives. | yes | yes |
| `{"type": "heard", "text": str}` | after a `listen` request, when the candidate's next turn ends (same VAD and turn rules as interviews). `text` is trimmed and may be `""` (the STT caught no words). One `heard` per `listen`. | – | yes |
| `{"type": "check_ended", "reason": "time_limit" \| "ended_by_client"}` | just before the service closes a check itself (then the connection drops). | – | yes |

Browser → server (`sendClientMessage`): `"listen"` (transcribe the next candidate turn; send again to repeat) and `"play_prompt"` (speak the sentence again; the **Play again** button). Unknown types and these types during interviews are ignored.

Check script: on connect the service speaks **"This is your interviewer. If you can hear me, press 'I heard it'."** The browser's `onBotStartedSpeaking`/`onBotStoppedSpeaking` and transcript events work as in sessions.

**Mic switching:** the service accepts **one offer per session or check**. During an interview, switch the mic with `RTCRtpSender.replaceTrack` on the existing connection (no new offer); a check can be switched the same way or ended and replaced by a new `POST /checks` (end waits, so the new create is never `busy`).

**Deviations from spec §3.1 (all additive, none contradicts it):** (a) `play_prompt` client message, needed by §3.2's **Play again**; (b) `GET /checks/{id}`, `PATCH /checks/{id}/offer` and the `check_ended` message; (c) `silent_input` also fires when no frames arrive (`reason: "no_audio"`), and carries `reason`; (d) `POST /checks/{id}/end` waits (≤5 s) for the lock to be free; (e) checks are SmallWebRTC only.

---

## File Structure

- Create `src/iassistpro/levels.py`: `LevelMeter` (pure PCM → messages) and `InputLevelMonitor` (frame processor + no-audio watchdog).
- Create `src/iassistpro/check.py`: `CHECK_PROMPT`, `CHECK_MAX_S`, `CheckController`, `run_check`.
- Modify `src/iassistpro/pipeline.py`: insert the monitor after `transport.input()`.
- Modify `src/iassistpro/config.py`, `src/iassistpro/turns.py`, `src/iassistpro/providers.py`: `BARGE_IN_MIN_S`.
- Modify `src/iassistpro/manager.py`, `src/iassistpro/api.py`: check sessions and routes.
- Modify `src/iassistpro/dev_client/client.html`: check mode and server-message counters.
- Tests: create `tests/test_levels.py`, `tests/test_check.py`; modify `tests/pipeline_harness.py`, `tests/test_pipeline.py`, `tests/test_turns.py`, `tests/test_config.py`, `tests/test_api.py`, `tests/conftest.py`.
- E2E: create `e2e/check.e2e.ts`; modify `e2e/lib/harness.ts`, `e2e/spoken.e2e.ts`; modify `scripts/make_session_wav.py` (`--noise`), `scripts/gen_candidate_clips.py` (`--only`); add clip `a12` to `tests/audio/candidate/answers.json` and `tests/audio/candidate/a12.wav`.
- Docs: `docs/contract-changes.md`, `README.md`.

---

### Task 1: Level meter and silent-input detection

**Files:**
- Create: `src/iassistpro/levels.py`
- Test: `tests/test_levels.py`

**Interfaces:**
- Produces:
  - Constants `LEVEL_WINDOW_S = 0.2`, `SILENT_AFTER_S = 3.0`.
  - `class LevelMeter` with `__init__(self, *, window_s: float = LEVEL_WINDOW_S, silent_after_s: float = SILENT_AFTER_S)`, `start(self, now: float) -> None` (arms the no-audio timer), `feed(self, pcm: bytes, sample_rate: int, channels: int, now: float) -> list[dict]` (int16 little-endian PCM; returns zero or more of `{"type": "mic_level", "rms": float}` / `{"type": "silent_input", "reason": "zeros"}`), `idle(self, now: float) -> list[dict]` (returns `[{"type": "silent_input", "reason": "no_audio"}]` once when started and `now - last_frame_or_start >= silent_after_s` and armed).
  - `class InputLevelMonitor(FrameProcessor)`: `__init__(self, meter: LevelMeter | None = None, clock: Callable[[], float] = time.monotonic, poll_s: float = 0.5)`; passes every frame through; for each `InputAudioRawFrame` pushes the meter's messages downstream as `RTVIServerMessageFrame(data=msg)`; `async def start_watchdog(self) -> None` (calls `meter.start(clock())` and starts a task that calls `meter.idle(clock())` every `poll_s`); `async def stop_watchdog(self) -> None`. Also cancels the watchdog on `EndFrame`/`CancelFrame`.
  - Semantics: a frame is "zero" only if **every** sample is 0. The zero run is counted in audio time (frame length), not wall time. Any non-zero frame resets the run and re-arms `silent_input` (both reasons share one arm flag).

- [ ] **Step 1: Write the failing tests** in `tests/test_levels.py` (10 ms frames at 16 kHz, mono unless stated; helper `frames(seconds, amplitude | "zeros" | "noise1")`):

```python
def test_level_every_200_ms_with_rms_in_full_scale_units():
    m = LevelMeter(); m.start(0)
    msgs = [x for i, f in enumerate(sine_frames(1.0, amp=0.1)) for x in m.feed(f, 16000, 1, i * 0.01)]
    levels = [x for x in msgs if x["type"] == "mic_level"]
    assert len(levels) == 5 and all(abs(x["rms"] - 0.0707) < 0.002 for x in levels)

def test_digital_silence_has_rms_zero():                      # levels all exactly 0.0
def test_silent_input_after_3_s_of_zero_samples():            # 2.99 s → none; at 3.00 s → exactly one {"type":"silent_input","reason":"zeros"}; 6 s more zeros → still one
def test_silent_input_rearms_after_sound():                    # 3 s zeros → 1; 0.1 s sine; 3 s zeros → 2 in total
def test_quiet_room_noise_is_never_silent_input():             # 60 s of random ±1..2 LSB samples (seeded; a frame may contain zeros, never all) → 0 silent_input, every rms > 0
def test_stereo_frames_are_measured():                         # channels=2, 1 s → 5 levels, rms ≈ 0.0707
def test_no_frames_for_3_s_is_silent_input():                  # start(0); idle(2.9) == []; idle(3.0) == [{"type":"silent_input","reason":"no_audio"}]; idle(10) == []
def test_no_audio_timer_restarts_on_each_frame():              # start(0); feed sine at t=2; idle(4.9) == []; idle(5.0) → one no_audio
def test_no_audio_and_zeros_share_one_episode():               # 3 s zeros (→ zeros msg), then no frames 3 s → idle gives nothing until a non-zero frame re-arms
async def test_monitor_pushes_server_message_frames_and_passes_audio():  # tiny pipeline: InputLevelMonitor between a source and a sink; 1 s of sine → sink sees 100 InputAudioRawFrames and 5 RTVIServerMessageFrame with data["type"] == "mic_level"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./scripts/dtest.sh pytest -q tests/test_levels.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'iassistpro.levels'`.

- [ ] **Step 3: Implement `levels.py`.** Use `numpy.frombuffer(pcm, dtype="<i2")`; accumulate sum of squares and sample count per window (window length = `window_s * sample_rate * channels` samples); `rms = round(sqrt(sumsq / n) / 32768, 4)`. Import `RTVIServerMessageFrame` from `pipecat.processors.frameworks.rtvi`.

- [ ] **Step 4: Run them to verify they pass**

Run: `./scripts/dtest.sh pytest -q tests/test_levels.py`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/iassistpro/levels.py tests/test_levels.py
git commit -m "feat: inbound mic level and silent-input detection"   # + trailer
```

---

### Task 2: Mic level and silent input in interview sessions

**Files:**
- Modify: `src/iassistpro/pipeline.py` (pipeline list and `_connected`/`finally`)
- Modify: `tests/pipeline_harness.py` (FakeInput audio + client messages; FakeOutput collects server messages; `run_scripted(..., on_connected=None)`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `InputLevelMonitor` (Task 1).
- Produces (harness, used by Task 4 too):
  - `FakeInput.audio(seconds: float, amplitude: float | None) -> Awaitable[None]` pushes 10 ms 16 kHz mono `InputAudioRawFrame`s (`None` = all zeros; no sleeping between frames except `await asyncio.sleep(0)`).
  - `FakeInput.client_message(type: str) -> Awaitable[None]` pushes `RTVIClientMessageFrame(msg_id=uuid4().hex, type=type, data=None)`.
  - `FakeOutput.server_messages: list[dict]` (the `data` of each `RTVIServerMessageFrame` reaching the output).
  - `run_scripted(..., on_connected: Callable[[FakeTransport], Awaitable[None]] | None = None, monitor_clock=None)`; hooks gain `.server_messages`.
  - `run_interview(..., level_monitor: InputLevelMonitor | None = None)`: default `InputLevelMonitor()`; placed directly after `transport.input()`; `start_watchdog()` in `_connected` before `hooks.on_live()`; `stop_watchdog()` in `finally`.

- [ ] **Step 1: Write the failing tests** in `tests/test_pipeline.py`:

```python
async def test_interview_sends_mic_level_messages(tmp_store, brief_3q):
    hooks = await run_scripted(brief_3q, tmp_store, ANSWERS_3, on_connected=lambda t: t.input().audio(1.0, 0.1))
    assert len([m for m in hooks.server_messages if m["type"] == "mic_level"]) >= 5

async def test_interview_reports_digital_silence(tmp_store, brief_3q):   # on_connected pushes 3.2 s of zeros → exactly one {"type":"silent_input","reason":"zeros"}; the session still ends "completed"
async def test_interview_reports_no_audio_after_connect(tmp_store, brief_3q):  # monitor with a fake clock advanced 3.5 s after connect and poll_s=0.01 → one {"type":"silent_input","reason":"no_audio"}
async def test_level_messages_never_reach_the_transcript(tmp_store, brief_3q):  # result turns identical to the run without audio
```

(Answers list: reuse whatever `test_scripted_session_produces_turns_ids_and_end_reason` uses; name it `ANSWERS_3` at module level if it is inline today.)

- [ ] **Step 2: Run them to verify they fail**

Run: `./scripts/dtest.sh pytest -q tests/test_pipeline.py`
Expected: the 4 new tests FAIL (`TypeError: unexpected keyword 'on_connected'`); existing tests pass.

- [ ] **Step 3: Implement the harness changes and the `run_interview` change** as the Interfaces block states.

- [ ] **Step 4: Run the pipeline and transcript suites**

Run: `./scripts/dtest.sh pytest -q tests/test_pipeline.py tests/test_transcript.py tests/test_levels.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/iassistpro/pipeline.py tests/pipeline_harness.py tests/test_pipeline.py
git commit -m "feat: interview sessions stream mic level and silent input"   # + trailer
```

---

### Task 3: `BARGE_IN_MIN_S` (UX5 fallback, default off)

**Files:**
- Modify: `src/iassistpro/config.py`, `src/iassistpro/turns.py`, `src/iassistpro/providers.py:253-267`, `tests/conftest.py` (add `BARGE_IN_MIN_S` to the cleared vars)
- Test: `tests/test_turns.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Settings.barge_in_min_s: float = Field(0.0, ge=0.0, le=3.0)` (env `BARGE_IN_MIN_S`).
  - `turns.BargeInFloorStartStrategy(BaseUserTurnStartStrategy)`, `__init__(self, *, min_s: float, **kwargs)`:
    - bot not speaking: `VADUserStartedSpeakingFrame` → `report_user_turn_started()`; a `TranscriptionFrame` with no turn started → `report_user_turn_started()` (the defaults' behaviour).
    - bot speaking (`BotStartedSpeakingFrame` … `BotStoppedSpeakingFrame`): speech counted from `VADUserStartedSpeakingFrame.start_secs` plus each `InputAudioRawFrame`'s duration while VAD says speaking; report once the count reaches `min_s`. `VADUserStoppedSpeakingFrame` before that → `trigger_reset_aggregation()` and mark the segment rejected; every `TranscriptionFrame`/`InterimTranscriptionFrame` until the next `VADUserStartedSpeakingFrame` is ignored (and resets aggregation) even if the bot has stopped meanwhile.
    - `handle_user_turn_started()` clears the counters.
  - `providers.make_turn_strategies(s)`: `start=[BargeInFloorStartStrategy(min_s=s.barge_in_min_s)]` when `s.barge_in_min_s > 0`, else `start=None` (Pipecat defaults, today's behaviour).

- [ ] **Step 1: Write the failing tests** (drive the strategy directly as `test_turns.py` drives the grace strategy; record `on_user_turn_started` and `on_reset_aggregation` events):

```python
def test_barge_in_floor_defaults_to_off():           # Settings().barge_in_min_s == 0.0; start types == [VADUserTurnStartStrategy, TranscriptionUserTurnStartStrategy]
def test_barge_in_floor_setting_selects_the_floor(monkeypatch):  # BARGE_IN_MIN_S=0.8 → start == [BargeInFloorStartStrategy] with min_s 0.8; BARGE_IN_MIN_S=4 → ValidationError
async def test_floor_starts_at_once_when_the_bot_is_silent():     # VAD start → 1 started event
async def test_floor_ignores_short_speech_while_the_bot_speaks(): # BotStarted; VAD start(0.2); 0.3 s audio; VAD stop → 0 started, 1 reset
async def test_floor_interrupts_after_min_s_of_speech():          # BotStarted; VAD start(0.2); 0.59 s → 0 started; 0.61 s → 1 started
async def test_floor_drops_the_late_transcript_of_rejected_speech():  # rejected burst; BotStopped; TranscriptionFrame("hello") → 0 started; next VAD start → 1 started
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./scripts/dtest.sh pytest -q tests/test_turns.py tests/test_config.py`
Expected: new tests FAIL (`ImportError: BargeInFloorStartStrategy`).

- [ ] **Step 3: Implement** the setting, the strategy and the `make_turn_strategies` change.

- [ ] **Step 4: Run them to verify they pass**

Run: `./scripts/dtest.sh pytest -q tests/test_turns.py tests/test_config.py tests/test_providers.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/iassistpro/config.py src/iassistpro/turns.py src/iassistpro/providers.py tests/conftest.py tests/test_turns.py tests/test_config.py
git commit -m "feat: optional barge-in floor for no-headset use (BARGE_IN_MIN_S, UX5)"   # + trailer
```

---

### Task 4: Check pipeline (no LLM)

**Files:**
- Create: `src/iassistpro/check.py`
- Test: `tests/test_check.py`

**Interfaces:**
- Consumes: `InputLevelMonitor` (Task 1); harness `FakeTransport`, `FakeTTS`, `Passthrough`, `external_strategies`, `FakeInput.audio/.client_message`, `FakeOutput.server_messages` (Task 2); `make_turn_strategies` (Task 3).
- Produces:
  - `CHECK_PROMPT = "This is your interviewer. If you can hear me, press 'I heard it'."`; `CHECK_MAX_S = 120`.
  - `CheckEndReason = Literal["ended_by_client", "disconnected", "time_limit", "error"]`.
  - `class CheckHooks(Protocol)`: `check_id: str`, `end_requested: asyncio.Event`, `on_live() -> None`, `on_end(reason: CheckEndReason, error: str | None) -> None`.
  - `class CheckController(FrameProcessor)`: drops `LLMContextFrame`s; on `RTVIClientMessageFrame` type `"listen"` sets `listening`; `"play_prompt"` → `await self.play_prompt()`; other types ignored (not forwarded). `async def play_prompt()` pushes `TTSSpeakFrame(CHECK_PROMPT)`. `async def on_user_turn(text: str | None)`: if listening, push `RTVIServerMessageFrame({"type": "heard", "text": (text or "").strip()})`, clear `listening`, `log_event("check_heard", check=…, chars=n)`.
  - `async def run_check(hooks: CheckHooks, transport, s: Settings, *, stt=None, tts=None, turn_strategies=None, vad=None, level_monitor=None, max_s: float = CHECK_MAX_S) -> None`. Pipeline `[transport.input(), monitor, stt, aggs.user(), controller, tts, transport.output()]`; `aggs = LLMContextAggregatorPair(LLMContext(), user_params=LLMUserAggregatorParams(vad_analyzer=vad, user_turn_strategies=turn_strategies))`, with `aggs.user()`'s `on_user_turn_stopped(agg, strategy, message)` → `controller.on_user_turn(message.content)`. `PipelineWorker(..., PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000), cancel_on_idle_timeout=False, idle_timeout_secs=None)`. On `on_client_connected`: `monitor.start_watchdog()`, `hooks.on_live()`, `controller.play_prompt()`, start the `max_s` timer. End paths: `end_requested` → push `{"type":"check_ended","reason":"ended_by_client"}`, sleep 0.2 s, `worker.cancel()`, reason `ended_by_client`; timer → same with `time_limit`; `on_client_disconnected` → `disconnected`; exception → `error`, `pipeline_error:<Type>`. `finally`: `stop_watchdog()`, `log_event("check_end", check=…, reason=…, ms=…)`, `hooks.on_end(...)`. **No import of `make_llm`; no store; no recorder.**

- [ ] **Step 1: Write the failing tests** in `tests/test_check.py` (helper `run_fake_check(**kw)` = `run_check` with `FakeTransport`, `Passthrough` STT, `FakeTTS`, `external_strategies()`, a `FakeCheckHooks`; a driver fires `on_client_connected` once the input's `start_event` is set):

```python
async def test_check_speaks_the_prompt_on_connect():       # FakeTTS saw TTSSpeakFrame text == CHECK_PROMPT; hooks.live
async def test_play_prompt_speaks_it_again():              # client_message("play_prompt") → 2 prompts
async def test_listen_then_a_turn_sends_heard():           # client_message("listen"); say("I am ready for my interview") → server_messages contains {"type":"heard","text":"I am ready for my interview"}
async def test_a_turn_without_listen_sends_nothing():      # say(...) with no listen → no "heard"
async def test_each_listen_gives_one_heard():              # listen, say A, say B → 1 heard; listen, say C → 2 heard
async def test_check_sends_mic_level_and_silent_input():   # audio(1.0, 0.1) → ≥5 mic_level; audio(3.2, None) → one silent_input zeros
async def test_check_never_builds_an_llm(monkeypatch):     # monkeypatch iassistpro.providers.make_llm to raise; full check runs; assert "make_llm" not in vars(iassistpro.check)
async def test_end_request_ends_the_check():               # end_requested.set() → on_end("ended_by_client", None); last server message {"type":"check_ended","reason":"ended_by_client"}
async def test_check_time_limit_ends_it():                  # max_s=0.3 → on_end("time_limit", None) and a check_ended time_limit message
async def test_disconnect_ends_the_check():                 # fire on_client_disconnected → on_end("disconnected", None)
async def test_check_logs_no_transcript(captured_logs):    # listen; say(f"ready {CANARY}") → CANARY not in captured_logs(); "check_heard" in logs
async def test_check_writes_nothing(tmp_path, monkeypatch):  # SESSIONS_DIR=tmp_path; after a full check tmp_path is empty
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./scripts/dtest.sh pytest -q tests/test_check.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'iassistpro.check'`.

- [ ] **Step 3: Implement `check.py`** per the Interfaces block.

- [ ] **Step 4: Run them to verify they pass**

Run: `./scripts/dtest.sh pytest -q tests/test_check.py tests/test_pipeline.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/iassistpro/check.py tests/test_check.py
git commit -m "feat: LLM-less audio check pipeline (prompt, listen, heard)"   # + trailer
```

---

### Task 5: Check sessions in the manager and the HTTP API

**Files:**
- Modify: `src/iassistpro/manager.py`, `src/iassistpro/api.py`, `docs/contract-changes.md`, `README.md`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `run_check`, `CheckHooks`, `CheckEndReason` (Task 4).
- Produces:
  - `UnknownCheck(ServiceError)`: 404 `unknown_check`. Constants `CHECK_END_WAIT_S = 5.0`, `CHECK_KEEP_S = 600`.
  - `class CheckSession` (implements `CheckHooks`): `check_id`, `token_hash`, `created_mono`, `ttl_s`, `expires_at`, `state`, `offered`, `pc_id`, `end_requested`, `task`, `end_reason`, `error`, `connected_mono`, `ended_mono`; `token_ok()`, `expired()`, `on_live()`, `on_end(reason, error)` (sets state `failed` for `error`, else `ended`; releases the lock; `log_event("check_ended", …)`), `expire()` (→ `failed`/`not_connected`), `status() -> dict` (the `CheckStatus` shape). No store calls.
  - `LiveSession.expire()`: today's `SessionManager._expire` body moved onto the session, so `_expire_waiting` and `_check_connect` call `active.expire()` for either kind.
  - `SessionManager.__init__(..., check_runner: CheckRunner | None = None)` (default `check.run_check`); `_active: LiveSession | CheckSession | None`; `_checks: dict[str, CheckSession]`.
  - `_start_runner(live, transport, runner)` and a shared `_answer_offer(live, body, runner) -> dict` used by `offer` and `check_offer`; `_check_connect(live, token)` takes the looked-up object.
  - `async create_check() -> dict` (busy/not_ready as `create`; `WrongState` when `transport != "smallwebrtc"`; ids from `new_session_id()`); `async check_offer(check_id, token, body)`, `async check_patch(check_id, token, body)`, `check_status(check_id) -> dict`, `async end_check(check_id) -> dict` (waiting and not offered → `on_end("ended_by_client")`; otherwise set `end_requested`, `await asyncio.wait_for(asyncio.shield(task), CHECK_END_WAIT_S)`, on timeout cancel the task and `on_end("ended_by_client", None)`; returns `check_status`).
  - `housekeeping` also drops checks whose `ended_mono` is older than `CHECK_KEEP_S`.
  - Routes exactly as "Contract additions": `POST /checks` (201), `GET /checks/{check_id}`, `POST`/`PATCH /checks/{check_id}/offer`, `POST /checks/{check_id}/end` (202).

- [ ] **Step 1: Write the failing tests** in `tests/test_api.py` (a `FakeCheckRunner` like `FakeRunner`: `on_live`, waits on `end_requested` or its `disconnect` event, `on_end`; a `stuck=True` flag ignores `end_requested`; `make()` passes `check_runner=`):

```python
async def test_create_check_returns_connect_info(client):      # 201; check_id 32 hex; connect.offer_path == f"/checks/{id}/offer"; token len ≥ 40; expires_at ≈ now+120 s
async def test_check_and_session_share_the_lock(client):       # check waiting → POST /sessions 409 busy, POST /checks 409 busy, health busy true; after end: session waiting → POST /checks 409 busy
async def test_check_not_ready_is_503(tmp_path):
async def test_check_offer_token_is_single_use(client):        # offer 200 → GET state live; 2nd offer 409 wrong_state; wrong token 401; PATCH same token 200
async def test_unused_check_expires_and_frees_the_lock(ctx):    # clock +121; housekeeping(once=True) → GET failed/not_connected; offer 410; POST /sessions 201
async def test_end_check_frees_the_lock_before_replying(client):  # offer; end → 202 state "ended" end_reason "ended_by_client"; immediately POST /sessions → 201
async def test_end_check_cancels_a_stuck_check_after_the_wait(tmp_path, monkeypatch):  # stuck runner, CHECK_END_WAIT_S=0.1 → 202 ended; POST /sessions 201
async def test_end_check_is_idempotent(client):                 # second end → 202 same status
async def test_disconnected_check_frees_the_lock(ctx):          # runner disconnect → state ended/disconnected; POST /checks 201
async def test_check_stores_nothing(ctx, tmp_path):             # full check; sessions dir has no entries
async def test_check_and_session_ids_do_not_cross(client):      # GET /sessions/{check_id} 404 unknown_session; GET /checks/{session_id} 404 unknown_check
async def test_unknown_check_404(client):                       # GET, end, offer
async def test_finished_checks_are_forgotten_after_10_minutes(ctx):  # end; clock +601; housekeeping → GET 404
async def test_checks_need_smallwebrtc(tmp_path):              # TRANSPORT=websocket → POST /checks 409 wrong_state
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./scripts/dtest.sh pytest -q tests/test_api.py`
Expected: new tests FAIL (404 on `/checks`); existing tests pass.

- [ ] **Step 3: Implement** the manager and API changes per the Interfaces block. Keep every existing session behaviour and test unchanged.

- [ ] **Step 4: Run the whole fast suite**

Run: `./scripts/dtest.sh pytest -q`
Expected: all pass (slow tests skipped).

- [ ] **Step 5: Document the contract.** Add four dated rows to `docs/contract-changes.md` (checks endpoints and `CheckStatus`; `mic_level`/`silent_input` on sessions and checks; `heard`/`check_ended` and the `listen`/`play_prompt` client messages; `BARGE_IN_MIN_S`), each "Added; `CONTRACT_VERSION` stays 1", rulings UX7 / §3.3 + UX3 / UX7 / UX5. Add a short "Audio check" section to `README.md` with the endpoints table and the message table from "Contract additions" (copy them).

- [ ] **Step 6: Commit**

```bash
git add src/iassistpro/manager.py src/iassistpro/api.py tests/test_api.py docs/contract-changes.md README.md
git commit -m "feat: check sessions over HTTP sharing the one-session lock and tokens (UX7)"   # + trailer
```

---

### Task 6: Spoken e2e of the check (ok and silent) and mic level in the session e2e

**Files:**
- Modify: `src/iassistpro/dev_client/client.html`, `e2e/lib/harness.ts`, `e2e/spoken.e2e.ts`, `scripts/make_session_wav.py`, `scripts/gen_candidate_clips.py`, `tests/audio/candidate/answers.json`, `tests/test_e2e_fixtures.py`
- Create: `e2e/check.e2e.ts`, `tests/audio/candidate/a12.wav`

**Interfaces:**
- Consumes: the HTTP and RTVI contract (Task 5).
- Produces:
  - Dev client: query `?check=<id>&token=…&dur=…` uses `/checks/{id}/offer` (else today's session mode). Both modes record `onServerMessage` into `state.server = {mic_level: n, mic_level_nonzero: n, silent_input: [{t, reason}], heard: [{t, text}], check_ended: [...]}` and `state.connectedAt`. Check mode sends `client.sendClientMessage("listen")` on the first `onBotStoppedSpeaking`, and finishes on the first `heard` with non-empty text or at `dur`. The hint line drops "Use headphones" for: "If the interviewer stops when you didn't speak, use headphones."
  - `harness.ts`: `runCheck(opts: { wav: string; durS: number; channel: string }): Promise<{ server: any; connectedAtMs: number; finalStatus: any }>` (creates via `POST /checks`, opens the dev client in check mode with the same browser flags as `runSession`, waits for `done`, then `POST /checks/{id}/end` and `GET /checks/{id}`); `writeSilentWav(path: string, seconds: number): void` (48 kHz mono PCM16 zeros). `runSession` also returns `server`.
  - `make_session_wav.py --noise <amplitude>` (default 0): adds seeded uniform noise of that amplitude to the whole file, so leads are a quiet room, not digital silence.
  - `gen_candidate_clips.py --only a12` voices only the listed ids (existing clips untouched).
  - Clip `a12`: text **"I am ready for my interview."**

- [ ] **Step 1: Add the clip.** Append `{"id": "a12", "text": "I am ready for my interview."}` to `answers.json`; add `--only`; run `./scripts/dtest.sh python scripts/gen_candidate_clips.py --only a12`. Check `git status` shows only `a12.wav` new among the clips. Extend `tests/test_e2e_fixtures.py` so every answers.json id has its WAV.

- [ ] **Step 2: Write the failing e2e tests** in `e2e/check.e2e.ts` (both free; skip if the service's LLM is not `fake` or `e2e/out/check_ok.wav` is missing):

```ts
test("audio check passes with a speaking fake mic (acceptance 2)", async () => {
  const r = await runCheck({ wav: CHECK_OK, durS: 40, channel });
  const heard = r.server.heard.find((h: any) => h.text.trim() !== "");
  expect(heard).toBeTruthy();
  expect(normalize(heard.text)).toEqual(expect.arrayContaining(["ready", "interview"]));
  expect(heard.t - r.connectedAtMs).toBeLessThan(30_000);
  expect(r.server.mic_level_nonzero).toBeGreaterThan(0);
  expect(r.server.silent_input).toEqual([]);           // quiet-room lead, never digital silence
  expect(r.finalStatus.state).toBe("ended");
});

test("audio check reports a silent input within 5 s (acceptance 1)", async () => {
  writeSilentWav(CHECK_SILENT, 20);
  const r = await runCheck({ wav: CHECK_SILENT, durS: 12, channel });
  expect(r.server.silent_input.length).toBeGreaterThanOrEqual(1);
  expect(r.server.silent_input[0].t - r.connectedAtMs).toBeLessThan(5_000);
  expect(r.server.mic_level_nonzero).toBe(0);
  expect(r.server.heard).toEqual([]);
});
```

In `spoken.e2e.ts`'s free test add `expect(r.server.mic_level).toBeGreaterThan(0)`.

- [ ] **Step 3: Build the WAV and run the e2e to verify it fails**

Run: `./scripts/dtest.sh python scripts/make_session_wav.py --clips a12 --lead 7 --gap 10 --noise 0.002 --out e2e/out/check_ok.wav`, then `LLM_PROVIDER=fake docker compose -f docker/compose.yml up -d --build interviewer` and `cd e2e && npx playwright test check.e2e.ts`
Expected: FAIL (`runCheck` not exported / dev client has no check mode).

- [ ] **Step 4: Implement** the dev client, harness and script changes.

- [ ] **Step 5: Run the e2e to verify it passes**

Run: `docker compose -f docker/compose.yml up -d --build interviewer` (still `LLM_PROVIDER=fake`), then `cd e2e && npx playwright test check.e2e.ts spoken.e2e.ts`
Expected: 3 passed, 1 skipped (the paid live test). Paste the check runs' `heard.t - connectedAtMs` and silent-input times into the commit message body.

- [ ] **Step 6: Full verification and stop the service**

Run: `./scripts/dtest.sh pytest -q` then `docker compose -f docker/compose.yml stop interviewer`
Expected: all pass; only `iassistpro-*` containers were touched.

- [ ] **Step 7: Commit**

```bash
git add src/iassistpro/dev_client/client.html e2e/check.e2e.ts e2e/lib/harness.ts e2e/spoken.e2e.ts scripts/make_session_wav.py scripts/gen_candidate_clips.py tests/audio/candidate/answers.json tests/audio/candidate/a12.wav tests/test_e2e_fixtures.py
git commit -m "test: spoken e2e of the audio check (heard, silent input) and session mic level"   # + trailer
```
