# IAssistPro Interviewer Service (Release 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution for this plan (decided):** inline via superpowers:executing-plans; one final whole-branch review on the most capable model. The architect agent approves for the owner (R10).

**Goal:** A standalone, generic voice mock-interviewer service: it accepts a session brief over HTTP, runs one live Pipecat voice interview in the browser (SmallWebRTC, local Whisper + Piper, Claude with tool calls), and hands back a transcript, per-model usage, timings and an Ogg/Opus recording. It knows nothing about JobLens.

**Architecture:** One Python package `src/iassistpro` served by FastAPI/uvicorn in one Docker image. A `SessionManager` owns the one-session lock, the one-time connect tokens and a per-session temp folder (`SessionStore`). The folder is written turn by turn (transcript, usage, state), so a crash still reports spend (AD4), and it expires after 24 h. Each live session runs a Pipecat pipeline built by `pipeline.py` from a provider factory (`providers.py`, env-driven, one paid TTS mapped). An `InterviewDirector` tracks the interviewer's tool calls (`ask_question`/`follow_up`/`end_interview`) and enforces the time, question, follow-up and cost-weighted limits. A content-free logging layer, with a canary test, keeps transcript, brief and audio out of logs.

**Tech Stack:** Python 3.12, uv, Pipecat 1.15.0 (`anthropic,kokoro,piper,silero,whisper,webrtc,websocket`), aiortc 1.15.0 / aioice 0.12.1 (patched), faster-whisper 1.2.1, piper-tts 1.8.0, FastAPI/uvicorn, pydantic 2 + pydantic-settings, loguru, soundfile; pytest + pytest-asyncio + httpx + jsonschema (dev); Docker Desktop/Compose; Playwright 1.56.1 + ajv (e2e, Node 22); Pipecat JS client 1.16.0 / small-webrtc-transport 1.11.2 / websocket-transport 1.8.2 (dev client page).

**Spec:** `C:\2026\rkumar\IAssistPro\docs\superpowers\specs\2026-11-04-mock-interviewer-design.md` (§4–6, §8, §10–12 are the authority for this plan). Decisions: `C:\2026\rkumar\IAssistPro\docs\rupak_decisions.md`, especially AS1–AS5 (spike rulings), A2 (one interviewer per stack) and AD4 (usage after every turn). Spike: `docs/spike-transport-latency.md` and `spike/` (reuse its aioice patch, Whisper/Piper/smart-turn setup, recorder, Playwright fake-mic harness and gap meter. Never modify or stage `spike/`.)

**Not in this plan (plan 2b, JobLens):** compose service in JobLens, `INTERVIEWER_URL`, offer proxying, `mock_sessions`, migration 0016, rating, spend recording, the live screen, `prod.ps1 -Interviewer`, tagging. The live dev session on Rupak's own voice (AS5 base.en re-check) also belongs to 2b. This plan only fixes the contract that 2b consumes ("Service interface" below).

## Global Constraints

- Repo `C:\2026\rkumar\IAssistPro`, branch `feat/mock-interviewer` (already checked out). Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. **Never push, merge or tag.** Stage explicit paths only (never `git add -A`/`.`); never stage `spike/`.
- **No personal data in IAssistPro (B2):** no CV, JD, transcript of a real person, recording of a real voice, key or email is ever committed. Fixtures use only the invented candidate "Sam" (Sam Rivera, backend engineer) and the invented company "Northwind Freight", reusing the spike's `ANSWERS`.
- **Keys:** never print, echo, log or copy a key value. The Anthropic key lives in `C:\2026\rkumar\JobLens\.env`. Only `scripts/up-live.sh` reads it, and it reads only the `ANTHROPIC_API_KEY=` line into its own process environment and passes it to compose **by name**, as the spike's `run_bot.sh` does. Tests read `ANTHROPIC_API_KEY` from the environment and only when `IASSIST_LIVE=1`.
- **Paid live tests are opt-in with a spend cap:** they run only with `IASSIST_LIVE=1`. Each live session's brief has `max_llm_cost_usd: 0.05`. The test fails if the session's cost (result usage × brief prices) exceeds `E2E_MAX_SPEND_USD` (default `0.10`), and it prints the cost. The whole plan's live spend stays under **$0.30**.
- **Never touch JobLens containers, volumes or prod.** IAssistPro's compose project is `iassistpro`. Standalone ports: HTTP `127.0.0.1:8770`, UDP `127.0.0.1:40020-40022`. These are distinct from JobLens prod 40000–40002 and dev 40010–40012 (AS4) and from ports 8000/8100. Only `iassistpro-*` containers are ever started, stopped or removed. `docker rm -f iassist-spike-bot` is allowed if it exists (IAssistPro's own spike container).
- **Pins (exact):** `pipecat-ai[anthropic,kokoro,piper,silero,whisper,webrtc,websocket]==1.15.0`, `aiortc==1.15.0`, `aioice==0.12.1`; the rest are locked in `uv.lock` with `[tool.uv] environments = ["sys_platform == 'linux'"]`. Python 3.12 (`python:3.12-slim`).
- **Speech defaults (AS1, AS2, AS5):** STT `faster-whisper base.en`, CPU, int8, beam 1, `STT_THREADS=4`. TTS Piper `en_US-lessac-medium`; Kokoro `af_heart` int8 is an option. TTS `stop_frame_timeout_s=10`. VAD Silero `stop_secs=0.2` plus `LocalSmartTurnAnalyzerV3`; the silence fallback is `SpeechTimeoutUserTurnStopStrategy` at 0.8 s total. LLM default `claude-haiku-5-5`, `max_tokens=200`, prompt caching on. **Never set a thinking parameter** (Pipecat's per-model lowest setting stays).
- **Tool calls never add an LLM round trip before speech:** handlers return `FunctionCallResultProperties(run_llm=False)`. The only exception is a response that spoke no text (Task 8).
- **Logs (§5.7, AS5):** INFO level. Only timings, sizes, counts, ids and error codes are logged, **never transcript text, brief content, SDP or audio**. loguru with `diagnose=False`. The uvicorn access log is off.
- **Contract:** `"version": 1`. JSON Schemas in `contracts/` are generated from the pydantic models and must match them (test). Example files are of the invented candidate.
- Tests run in the dev container from Git Bash at the repo root: `./scripts/dtest.sh pytest -q <files>` (= `docker compose -f docker/compose.yml --profile test run --rm tests …`). Slow tests are marked `@pytest.mark.slow` and skipped unless `-m slow` is passed. E2E runs from `e2e/`: `npx playwright test`.

## Review Focus

1. **The model calls a tool without saying anything** (a `tool_use` block with no text before it). Because tool results don't re-run the LLM, the candidate would sit in silence. Expected: the director sees a response with no spoken text and returns that one tool result with `run_llm=True`, so the interviewer speaks. Test: `test_tool_call_without_text_triggers_one_llm_run` (Task 8) and `test_scripted_session_tool_first_still_speaks` (Task 10).
2. **The browser goes away mid-session** (tab closed, network drop, laptop sleeps). Expected: the session ends `disconnected`, the result has every turn so far and the usage, the audio up to that point is encoded, and the lock is free for a new session. Test: `test_disconnect_mid_session_keeps_turns_and_frees_lock` (Task 11) and `test_disconnect_ends_disconnected_with_partial_transcript` (Task 10).
3. **The service dies during a session** (OOM, `docker restart`, crash). Expected: on startup the session reads as `failed` with `error: "service_restarted"`, with its persisted turns and usage, so the spend isn't lost (AD4). The lock is free. Test: `test_recover_on_startup_marks_live_failed_with_usage` (Task 4).
4. **A brief that should never reach the model** contains an email, URL or phone number, oversize limits, duplicate ids, 0 or 30 questions, or a wrong version. Expected: 422 with the field named and nothing logged. A year range like "2019-2023" is **not** treated as a phone number. Tests: `test_brief_rejects_contact_details`, `test_year_ranges_are_not_phone_numbers` (Task 2), `test_invalid_brief_422_logs_no_content` (Task 11).
5. **Start pressed twice, or a stale or replayed connect.** Cases: a second `POST /sessions` while one is waiting or live, a second offer with the same token, an offer after 120 s, an unknown token. Expected: 409 `busy` with the current id, 409 `wrong_state`, 410 `token_expired` (session `failed`, lock freed), and 401. Tests: `test_second_session_while_waiting_is_busy`, `test_offer_token_is_single_use`, `test_unused_session_expires_and_frees_lock` (Task 11).

---

## Service interface (the contract plan 2b consumes)

Container listens on `PORT` (default `8000`). Media uses the UDP range in `ICE_PORTS`, **published 1:1** (host port = container port, because the advertised candidate is `ICE_HOST:<port>`). Per stack (AS4): prod `40000-40002`, dev `40010-40012`, standalone `40020-40022`. Volumes: `/models` (model weights, `interviewer_models`) and `/data/sessions` (temp results, `interviewer_sessions`).

**Environment (one value each; defaults shown):**

| Var | Default | Notes |
|---|---|---|
| `PORT` | `8000` | HTTP |
| `TRANSPORT` | `smallwebrtc` | or `websocket` (fallback, no patch) |
| `ICE_PORTS` | `40010-40012` | must equal the published UDP range |
| `ICE_HOST` | `127.0.0.1` | advertised host candidate |
| `STT_PROVIDER` / `STT_MODEL` / `STT_BEAM` / `STT_THREADS` | `whisper` / `base.en` / `1` / `4` | `small.en` is the fallback model |
| `TTS_PROVIDER` / `TTS_VOICE` / `TTS_THREADS` | `piper` / provider default / `4` | `piper` (`en_US-lessac-medium`), `kokoro` (`af_heart`), `cartesia` (paid; `TTS_VOICE` required) |
| `TURN_STOP` / `SILENCE_S` | `smart` / `0.8` | `silence` = fallback |
| `LLM_PROVIDER` / `LLM_MODEL` | `anthropic` / `claude-haiku-5-5` | `fake` = scripted, test only; brief's `llm_model` overrides `LLM_MODEL` |
| `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY` | – | read only when that provider is selected |
| `MODELS_DIR` / `SESSIONS_DIR` | `/models` / `/data/sessions` | |
| `RESULT_TTL_H` / `CONNECT_TOKEN_TTL_S` | `24` / `120` | |
| `DEV_CLIENT` | `0` | `1` serves `/dev/client` (standalone and e2e only) |
| `DEBUG_STEREO_WAV` | `0` | `1` keeps the stereo WAV next to the Ogg |
| `LOG_LEVEL` | `INFO` | |

**Endpoints** (errors are `{"detail": {"code": "<code>", "message": "<text>", ...}}`):

| Method, path | Success | Errors |
|---|---|---|
| `GET /health` | 200 `Health` (always 200 while the process is up) | – |
| `POST /sessions` body `Brief` | 201 `{session_id, connect}` | 422 `invalid_brief`; 409 `busy` (+`session_id` of the waiting/live one); 503 `not_ready` (+`status`) |
| `GET /sessions/{id}` | 200 `Status` | 404 `unknown_session` |
| `POST /sessions/{id}/offer` (SmallWebRTC SDP offer JSON; token in `X-Session-Token` header or `?token=`) | 200 SDP answer JSON (Pipecat's `{sdp, type, pc_id}`) | 401 `token_invalid`; 410 `token_expired`; 409 `wrong_state` |
| `PATCH /sessions/{id}/offer` (`{pc_id, candidates}`, same token) | 200 `{"status":"success"}` | as above |
| `WS /sessions/{id}/ws?token=` (`TRANSPORT=websocket` only; Pipecat protobuf serializer) | – | close 4401/4410/4409 |
| `POST /sessions/{id}/end` | 202 `Status` (idempotent) | 404 |
| `GET /sessions/{id}/result` | 200 `Result` (states `ended`/`failed`) | 409 `not_finished`; 404 |
| `GET /sessions/{id}/audio` | 200 `audio/ogg` bytes, `Content-Length` | 409 `audio_pending`; 404 `no_audio`/`unknown_session` |
| `DELETE /sessions/{id}` | 204 (removes the temp folder) | 409 `wrong_state` while waiting/live; 404 |
| `GET /dev/client` | 200 HTML (only with `DEV_CLIENT=1`) | 404 |

`connect` is `{"transport": "smallwebrtc", "offer_path": "/sessions/{id}/offer", "token": "<one-time>", "expires_at": "<ISO 8601 UTC>"}`. For websocket it is `{"transport": "websocket", "ws_path": "/sessions/{id}/ws", "token": "…", "expires_at": "…", "audio": {"in_sample_rate": 16000, "out_sample_rate": 24000}}`. A token works for one offer within `CONNECT_TOKEN_TTL_S`; after that the same token is accepted only for `PATCH` on that session's `pc_id` until it ends.

`Health`: `{"status": "loading_models" | "ready" | "error", "version": "0.1.0", "contract_version": 1, "progress": {"done_mb": int, "total_mb": int, "current": str} | null, "error": null | "missing_key:<VAR>" | "model_download_failed:<asset>" | "config:<message>", "providers": {"stt": {"name", "model", "remote": bool}, "tts": {"name", "voice", "remote": bool}, "llm": {"name", "model"}, "transport": "smallwebrtc" | "websocket"}, "live_session": null | "<id>"}`. A down service is "connection refused"; 2b maps the three states to its §7.3 lines.

`Status`: `{"session_id", "state": "waiting" | "live" | "ended" | "failed", "elapsed_s": float, "current_question_id": str | null, "question_number": int, "questions_total": int, "turn_kind": "greeting" | "main" | "follow_up" | "closing" | null, "end_reason": EndReason | null, "error": str | null, "audio_state": "pending" | "ready" | "none" | "failed"}`. Lifecycle: `waiting` → (offer) `live` → `ended`. Error paths go to `failed`: `waiting` → `failed` (`not_connected`, after the token TTL), and a crash or restart → `failed`. Audio goes `pending` → `ready` about a minute after `ended`. The result is available immediately and doesn't wait for the audio (AS5).

**Also part of the contract (architect APR1, reconciled with plan 2b's C1–C10):**
- Live captions are Pipecat RTVI events on the WebRTC data channel (`onUserTranscript` with `final`, `onBotTranscript`). The pipeline enables RTVI transcript events, and the bot transcript carries spoken text only (never notes or tool calls).
- Every candidate turn carries the interviewer's latest `question_id` and `kind` (`question_id` null before the first question).
- Usage `input_tokens` is Anthropic's uncached input (it excludes cache read and cache write tokens).
- JobLens sends the token server-side in `X-Session-Token`; `?token=` exists only for the dev client.
- The e2e session WAVs are generated into `e2e/out/` (git-ignored) by `scripts/make_session_wav.py`. Plan 2b's live test uses `e2e/out/session_live.wav`.

`Brief` and `Result`: Task 2 Interfaces (exact fields); schemas `contracts/brief.v1.schema.json`, `contracts/result.v1.schema.json`; examples `contracts/examples/brief.example.json`, `contracts/examples/result.example.json`.

---

### Task 1: Project scaffold, dev container and settings

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `.dockerignore`, `src/iassistpro/__init__.py`, `src/iassistpro/config.py`, `docker/Dockerfile`, `docker/compose.yml`, `scripts/dtest.sh`, `tests/conftest.py`, `tests/test_config.py`
- Modify: `.gitignore` (add `e2e/out/`, `e2e/node_modules/`, `e2e/test-results/`, `e2e/playwright-report/`, `.venv/`)

**Interfaces:**
- Produces:
  - `iassistpro.__version__ = "0.1.0"`; `CONTRACT_VERSION = 1`.
  - `config.Settings(BaseSettings)`: fields and defaults exactly as the env table above (`ice_ports: str`, `stt_threads: int`, …). Also `ice_range: tuple[int, int]` (property; parsed `"40010-40012"` → `(40010, 40012)`; `ValueError` if lo > hi or the range has more than 10 ports), `tts_voice_resolved: str` (property: provider default when `TTS_VOICE` is empty) and `vad_stop_s: float = 0.2` (constant, not env).
  - `config.ConfigError(Exception)`.
  - `docker/Dockerfile` stages: `base` (python:3.12-slim + apt `ffmpeg libsndfile1 espeak-ng` + uv; `uv sync --frozen --no-dev --no-install-project`; `ENV HF_HOME=/models/hf MODELS_DIR=/models SESSIONS_DIR=/data/sessions PYTHONUNBUFFERED=1`), `dev` (adds the dev group; `PYTHONPATH=/app/src`; source is mounted), `runtime` (Task 12).
  - `docker/compose.yml`: `name: iassistpro`; service `tests` (profile `test`, `target: dev`, volumes `..:/app`, `interviewer_models:/models`, `working_dir: /app`); volumes `interviewer_models`, `interviewer_sessions`.
  - `scripts/dtest.sh "$@"` → `MSYS_NO_PATHCONV=1 docker compose -f docker/compose.yml --profile test run --rm tests "$@"`.
  - pytest config: `asyncio_mode = "auto"`, marker `slow` (skipped unless `-m slow`), `testpaths = ["tests"]`.

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`:

```python
def test_defaults_match_the_spike_rulings(monkeypatch):
    s = Settings(_env_file=None)
    assert (s.transport, s.stt_provider, s.stt_model, s.stt_beam, s.stt_threads) == ("smallwebrtc", "whisper", "base.en", 1, 4)
    assert (s.tts_provider, s.tts_voice_resolved, s.turn_stop, s.llm_model) == ("piper", "en_US-lessac-medium", "smart", "claude-haiku-5-5")
    assert (s.result_ttl_h, s.connect_token_ttl_s, s.ice_range, s.vad_stop_s) == (24, 120, (40010, 40012), 0.2)

def test_kokoro_default_voice(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "kokoro"); assert Settings().tts_voice_resolved == "af_heart"

@pytest.mark.parametrize("bad", ["40012-40010", "abc", "40000-40020"])
def test_bad_ice_range_is_refused(monkeypatch, bad):
    monkeypatch.setenv("ICE_PORTS", bad)
    with pytest.raises(ValueError): Settings().ice_range
```

- [ ] **Step 2: Build the dev image and run the tests to verify they fail**

Run: `./scripts/dtest.sh pytest -q tests/test_config.py`
Expected: FAIL on `ImportError: cannot import name 'Settings'` (the image builds).

- [ ] **Step 3: Implement `pyproject.toml`, `uv lock`, `config.py`.** Use `pydantic-settings`; env names are the upper-case field names. Run `uv lock` on the host (the lock is Linux-only through `environments`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./scripts/dtest.sh pytest -q tests/test_config.py`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .dockerignore .gitignore src/iassistpro/__init__.py src/iassistpro/config.py docker/Dockerfile docker/compose.yml scripts/dtest.sh tests/conftest.py tests/test_config.py
git commit -m "feat: interviewer package scaffold, dev container and settings"   # + trailer
```

---

### Task 2: Brief and result contract, JSON Schemas and examples

**Files:**
- Create: `src/iassistpro/contract.py`, `contracts/brief.v1.schema.json`, `contracts/result.v1.schema.json`, `contracts/examples/brief.example.json`, `contracts/examples/result.example.json`, `scripts/export_schemas.py`
- Test: `tests/test_contract.py`

**Interfaces:**
- Produces (pydantic v2, `model_config = ConfigDict(extra="forbid")` on every model):
  - `Role{title: str 1..120, company: str 1..120, summary: str 1..2000}`; `Candidate{name: str 1..40, no digits, '@', '/' or ':', summary: str 1..3000}`.
  - `QuestionKind = Literal["must_have", "cv_deep_dive", "gap", "behavioural", "motivation", "own"]` (`own` = JobLens release 1's owner-added question).
  - `Question{id: str ^[A-Za-z0-9_-]{1,32}$, text: str 1..500, kind: QuestionKind, probe_for: list[str 1..200] 0..8 = []}`.
  - `Limits{max_minutes: int 1..45 = 25, max_main_questions: int 1..15 = 8, max_follow_ups: int 0..3 = 2, max_llm_cost_usd: float >0 ≤2.0 = 0.50}`.
  - `Prices{input, cached_input, cache_write, output: float ≥0 ≤100}` (USD per million tokens).
  - `Brief{version: Literal[1], role, candidate, questions: list[Question] 1..20 (unique ids), limits: Limits = Limits(), llm_prices_per_mtok: Prices, llm_model: str ^[a-z0-9.-]{1,64}$ | None = None, language: Literal["en"] = "en"}`. Its contact guard is a model validator over every string: an email (`\S+@\S+\.\S+`), a URL (`https?://` or `www.`), or a run of `[\d\s().+-]` holding ≥ 10 digits fails with `"contact details are not allowed in a brief (<dotted field path>)"`.
  - `Brief.planned_questions -> list[Question]`: the first `limits.max_main_questions` questions.
  - `EndReason = Literal["completed", "time_limit", "budget", "ended_by_candidate", "disconnected", "error"]`.
  - `Turn{speaker: Literal["interviewer", "candidate"], text: str, start_ms: int ≥0, end_ms: int ≥0, question_id: str | None, kind: Literal["main", "follow_up", "greeting", "closing"]}`.
  - `Usage{kind: Literal["llm", "stt", "tts"] = "llm", provider: str, model: str, input_tokens: int = 0, cached_input_tokens: int = 0, cache_write_tokens: int = 0, output_tokens: int = 0, seconds: float | None = None, characters: int | None = None}`.
  - `AudioInfo{state: Literal["pending", "ready", "none", "failed"], format: Literal["ogg_opus", "wav"] | None, duration_ms: int = 0, bytes: int = 0}`; `Latency{n: int, median_reply_ms: int | None, p90_reply_ms: int | None, measured: Literal["server"] = "server"}`.
  - `Result{version: Literal[1], session_id, end_reason: EndReason, error: str | None, started_at: datetime | None, ended_at: datetime, turns: list[Turn], usage: list[Usage], llm_cost_usd: float, audio: AudioInfo, latency: Latency}`.
  - `llm_cost_usd(usage: list[Usage], prices: Prices) -> float` = Σ over `kind == "llm"` of `(input·p.input + cached_input·p.cached_input + cache_write·p.cache_write + output·p.output) / 1e6`. Pipecat's `prompt_tokens` is Anthropic's uncached `input_tokens`.
  - `export_schemas() -> dict[str, dict]` → `{"brief.v1.schema.json": Brief.model_json_schema(), "result.v1.schema.json": Result.model_json_schema()}`. The brief schema's `description` states the contact rule.
- Example brief: Backend Engineer at Northwind Freight, candidate "Sam", 6 questions (`q1` tell me about yourself … `q6` why this role), default limits, `llm_model: "claude-haiku-5-5"`, Haiku 5.5 prices `{"input": 0.10, "cached_input": 0.01, "cache_write": 0.125, "output": 0.50}`. Example result: 1 greeting, 2 main questions with 1 follow-up, closing, matching candidate turns, one Haiku usage row, `audio.state = "ready"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_example_brief_and_result_validate_with_pydantic_and_jsonschema():
    for name, model in [("brief", Brief), ("result", Result)]:
        data = json.loads((CONTRACTS / "examples" / f"{name}.example.json").read_text())
        model.model_validate(data)
        jsonschema.validate(data, json.loads((CONTRACTS / f"{name}.v1.schema.json").read_text()))

def test_committed_schemas_match_the_models():
    for fname, schema in export_schemas().items():
        assert json.loads((CONTRACTS / fname).read_text()) == schema

@pytest.mark.parametrize("mutate, field", [
    (lambda b: b.update(version=2), "version"),
    (lambda b: b.update(questions=[]), "questions"),
    (lambda b: b.update(questions=b["questions"] * 4), "questions"),          # 24 > 20 and duplicate ids
    (lambda b: b["limits"].update(max_minutes=90), "max_minutes"),
    (lambda b: b["limits"].update(max_llm_cost_usd=5), "max_llm_cost_usd"),
    (lambda b: b["questions"][0].update(text="x" * 501), "text"),
    (lambda b: b.update(extra="no"), "extra"),
])
def test_brief_validation_rejects(mutate, field): ...   # ValidationError whose str names `field`

@pytest.mark.parametrize("text", ["write to sam@example.org", "see https://example.org", "call +44 20 7946 0958", "www.example.org"])
def test_brief_rejects_contact_details(text):   # put `text` in candidate.summary → error mentions "contact details" and "candidate.summary"

def test_year_ranges_are_not_phone_numbers():   # "2019-2023, then 2023 - 2026" in role.summary validates

def test_planned_questions_respects_max_main_questions():   # max_main_questions=2 → ids ["q1", "q2"]

def test_llm_cost_uses_all_four_prices():
    u = [Usage(provider="anthropic", model="m", input_tokens=1_000_000, cached_input_tokens=2_000_000,
               cache_write_tokens=1_000_000, output_tokens=1_000_000),
         Usage(kind="tts", provider="cartesia", model="v", characters=500)]
    assert llm_cost_usd(u, Prices(input=0.10, cached_input=0.01, cache_write=0.125, output=0.50)) == pytest.approx(0.745)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./scripts/dtest.sh pytest -q tests/test_contract.py`
Expected: FAIL (`ModuleNotFoundError: iassistpro.contract`).

- [ ] **Step 3: Implement `contract.py`; write both examples; generate the schemas with `./scripts/dtest.sh python scripts/export_schemas.py`** (it writes `contracts/*.v1.schema.json`, sorted keys, 2-space indent).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./scripts/dtest.sh pytest -q tests/test_contract.py`
Expected: all pass.

- [ ] **Step 5: Commit** `src/iassistpro/contract.py contracts/ scripts/export_schemas.py tests/test_contract.py` with message `feat: versioned brief/result contract with JSON Schemas and invented-candidate examples` (plus the trailer).

---

### Task 3: Content-free logging

**Files:**
- Create: `src/iassistpro/logs.py`
- Test: `tests/test_logs.py`

**Interfaces:**
- Produces:
  - `setup_logging(level: str = "INFO") -> None`. It removes loguru's default sink and adds stderr with `diagnose=False, backtrace=False`, plus the filter below. Stdlib `logging` (uvicorn, aiortc, aioice, httpx) is routed into loguru through an intercept handler. `uvicorn.access` is disabled. Repeat calls are safe.
  - Filter rule: records from `iassistpro.*` pass at ≥ `level`. Records from any other module (`pipecat.*`, `aiortc`, `aioice`, `uvicorn`, `httpx`, …) pass only at ≥ WARNING.
  - `log_event(event: str, **fields) -> None` logs `event k=v …` at INFO. Allowed field values are `int | float | bool | None`, or `str` matching `^[A-Za-z0-9_.:/-]{0,64}$` (ids, codes, enum names). Any other value is written as `<redacted:len=N>`.
  - `CANARY = "ZEBRA-CANARY-7731"` (test helper constant in `tests/conftest.py`), and the fixture `captured_logs` (a list sink at DEBUG installed *after* `setup_logging`, using the same filter) that yields the joined text.

- [ ] **Step 1: Write the failing tests**

```python
def test_log_event_redacts_free_text(captured_logs):
    log_event("turn_closed", speaker="candidate", chars=42, text=f"I led {CANARY} the team")
    out = captured_logs()
    assert "turn_closed" in out and "chars=42" in out and CANARY not in out and "<redacted:len=" in out

def test_third_party_info_and_debug_are_dropped(captured_logs):
    from loguru import logger as lg
    lg.bind().patch(lambda r: r.update(name="pipecat.services.tts_service")).info(f"Generating TTS [{CANARY}]")
    logging.getLogger("aioice.ice").info(CANARY); logging.getLogger("uvicorn.access").info(CANARY)
    assert CANARY not in captured_logs()

def test_third_party_warning_passes(captured_logs):
    logging.getLogger("aiortc").warning("ice failed code=7"); assert "ice failed code=7" in captured_logs()

def test_exceptions_do_not_print_local_variables(captured_logs):
    secret = CANARY  # noqa: F841
    try: 1 / 0
    except ZeroDivisionError: logger.exception("boom")
    assert CANARY not in captured_logs()
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_logs.py`. Expected: FAIL (import).
- [ ] **Step 3: Implement `logs.py`.**
- [ ] **Step 4: Run the tests to verify they pass.** Same command. Expected: 4 passed.
- [ ] **Step 5: Commit** `src/iassistpro/logs.py tests/test_logs.py tests/conftest.py`, message `feat: content-free logging with redacting log_event`.

---

### Task 4: Session temp store, usage persistence, TTL and restart recovery

**Files:**
- Create: `src/iassistpro/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `contract.Turn`, `Usage`, `Prices`, `Limits`, `Result`, `AudioInfo`, `Latency`, `llm_cost_usd` (Task 2).
- Produces:
  - Layout `{root}/{session_id}/`: `meta.json` (`created_at`, `prices`, `limits`, `llm_model`, `questions_total`; **no brief text**); `state.json` (`state`, `end_reason`, `error`, `started_at`, `ended_at`, `audio`, `latency`); `turns.jsonl` (one `Turn` per line, appended); `usage.json` (full `list[Usage]`, replaced atomically); `audio.raw` (transient stereo PCM16 16 kHz); `audio.ogg`.
  - `SessionMeta`, `StateRecord` (pydantic; `StateRecord.state: Literal["waiting", "live", "ended", "failed"]`).
  - `SessionStore(root: Path, ttl_h: int)` with `create(session_id: str, meta: SessionMeta) -> None`, `write_state(session_id, rec: StateRecord) -> None` (atomic), `read_state(session_id) -> StateRecord`, `append_turn(session_id, turn: Turn) -> None` (append + flush + fsync), `write_usage(session_id, usage: list[Usage]) -> None` (write `usage.json.tmp`, `os.replace`), `result(session_id) -> Result`, `dir(session_id) -> Path`, `exists(session_id) -> bool`, `delete(session_id) -> None`, `sweep(now: datetime) -> list[str]` and `recover_on_startup(now: datetime) -> list[str]`.
  - `sweep` deletes a folder whose `ended_at` (else `created_at`) is older than `ttl_h`.
  - `recover_on_startup` turns `waiting`/`live` states into `failed`, with `end_reason="error"` and `error="service_restarted"`. It sets `ended_at=now`, and `audio.state="none"` unless `audio.ogg` exists. It deletes a leftover `audio.raw` unless `DEBUG_STEREO_WAV`.
  - `result()` builds `Result` from the files. `llm_cost_usd` is computed with `meta.prices`. A missing or corrupt `usage.json` gives `usage=[]` with `error` kept, and never raises.

- [ ] **Step 1: Write the failing tests**

```python
def test_usage_is_replaced_atomically_after_each_turn(tmp_path):   # write 1 row then 2 rows → result().usage has 2; no *.tmp left
def test_turns_append_in_order_and_survive_reopen(tmp_path):        # new SessionStore on same root reads 3 turns in order
def test_recover_on_startup_marks_live_failed_with_usage(tmp_path):
    st = SessionStore(tmp_path, 24); st.create("s1", meta()); st.write_state("s1", live())
    st.append_turn("s1", turn("candidate")); st.write_usage("s1", [haiku_usage(input_tokens=1000, output_tokens=100)])
    assert SessionStore(tmp_path, 24).recover_on_startup(NOW) == ["s1"]
    r = SessionStore(tmp_path, 24).result("s1")
    assert (r.end_reason, r.error, len(r.turns), r.audio.state) == ("error", "service_restarted", 1, "none")
    assert r.llm_cost_usd == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)
def test_ended_sessions_are_not_touched_by_recovery(tmp_path):
def test_sweep_deletes_after_ttl_only(tmp_path):                     # ended 23h59m ago kept; 24h01m deleted
def test_corrupt_usage_file_gives_empty_usage_not_crash(tmp_path):
def test_meta_holds_no_brief_text(tmp_path):                         # build meta from example brief → CANARY in summaries absent from meta.json
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_store.py`. Expected: FAIL (import).
- [ ] **Step 3: Implement `store.py`.** Timestamps are UTC ISO 8601. Writes go through a `_atomic_write(path, text)` helper.
- [ ] **Step 4: Run the tests to verify they pass.** Same command. Expected: 7 passed.
- [ ] **Step 5: Commit** `src/iassistpro/store.py tests/test_store.py`, message `feat: per-session temp store with per-turn usage, TTL sweep and restart recovery`.

---

### Task 5: Limits and the cost-weighted cap

**Files:**
- Create: `src/iassistpro/limits.py`
- Test: `tests/test_limits.py`

**Interfaces:**
- Consumes: `Limits`, `Prices`, `Usage`, `llm_cost_usd` (Task 2).
- Produces:
  - `CostMeter(prices: Prices, cap_usd: float)` with `add(u: Usage) -> float` (returns the turn's cost; accumulates per `(provider, model)`), `spent_usd: float`, `last_turn_usd: float`, `rows() -> list[Usage]` (summed per provider/model; local STT/TTS never appear), `near_cap() -> bool` (`spent ≥ 0.8·cap`) and `next_turn_would_exceed() -> bool` (`spent + 1.25·last_turn > cap`).
  - `LimitTracker(limits: Limits, planned_ids: list[str])` with `on_ask(qid) -> None`, `on_follow_up(qid) -> None`, `follow_ups(qid) -> int`, `main_asked: int`, `follow_up_limit_reached(qid) -> bool` and `last_main_asked() -> bool` (true once every planned id has been asked). `time_state(elapsed_s: float) -> Literal["ok", "wrap_up", "hard_stop"]`: `wrap_up` at `elapsed ≥ max_minutes·60`, `hard_stop` at `≥ max_minutes·60 + 90`.
  - `NOTE_PREFIX = "[Interviewer note, not said by the candidate] "` and the notes (exact copy):
    - `NOTE_NO_MORE_FOLLOW_UPS = "No more follow-ups on {qid}; move on to the next main question."`
    - `NOTE_LAST_MAIN = "That was the last main question. After this answer and any follow-ups, close the interview in one sentence and call end_interview with reason completed."`
    - `NOTE_TIME_UP = "Time is up. Thank the candidate and close the interview in one sentence now, then call end_interview with reason time_limit."`
    - `NOTE_BUDGET = "Close the interview in one sentence now, then call end_interview with reason budget."`
  - Fixed closings (spoken by TTS without an LLM call):
    - `CLOSE_TIME = "We're out of time, so let's stop here. Thank you, {name}, that's the end of the interview."`
    - `CLOSE_BUDGET = "Let's stop here. Thank you, {name}, that's the end of the interview."`
    - `CLOSE_ENDED = "Thanks, {name}. We'll stop here."`

- [ ] **Step 1: Write the failing tests**

```python
def test_meter_accumulates_and_projects():
    m = CostMeter(HAIKU, cap_usd=0.001)
    m.add(llm(input_tokens=2000, output_tokens=200))            # 0.0003
    assert m.spent_usd == pytest.approx(0.0003) and not m.near_cap() and not m.next_turn_would_exceed()
    m.add(llm(input_tokens=2000, output_tokens=200)); m.add(llm(input_tokens=2000, output_tokens=200))   # 0.0009
    assert m.near_cap() and m.next_turn_would_exceed()
def test_cached_reads_are_cheap_so_a_long_session_fits():
    # 40 turns of 200 uncached + 6000 cached input + 60 output on Haiku prices stay under the default 0.50 cap, and under 0.05
def test_rows_sum_per_model(): ...
def test_time_states(): # max_minutes=25: 1499 ok, 1500 wrap_up, 1589 wrap_up, 1590 hard_stop
def test_follow_up_limit_and_last_main(): # max_follow_ups=2 on q2 → reached after 2; last_main_asked after q1..qN
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_limits.py`. Expected: FAIL.
- [ ] **Step 3: Implement `limits.py`** (pure Python, no Pipecat import).
- [ ] **Step 4: Run the tests to verify they pass.** Expected: 5 passed.
- [ ] **Step 5: Commit** `src/iassistpro/limits.py tests/test_limits.py`, message `feat: cost-weighted cap, time and question limits`.

---

### Task 6: Provider factory (local defaults, one paid TTS mapped)

**Files:**
- Create: `src/iassistpro/providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `Settings`, `ConfigError` (Task 1); `Brief` (Task 2).
- Produces:
  - `ProviderSpec(kind: Literal["stt", "tts", "llm"], name: str, remote: bool, key_env: str | None, build: Callable[[Settings, ...], object])`. `REGISTRY: dict[tuple[str, str], ProviderSpec]` holds `("stt", "whisper")`, `("tts", "piper")`, `("tts", "kokoro")`, `("tts", "cartesia")` (remote, `CARTESIA_API_KEY`), `("llm", "anthropic")` (remote, `ANTHROPIC_API_KEY`) and `("llm", "fake")`. Adding a paid provider = one entry.
  - `required_keys(s: Settings) -> list[str]`: the `key_env` of the selected providers that are missing from the environment. Only names are returned, never values.
  - `provider_info(s: Settings) -> dict`: the `providers` block of `Health`.
  - `make_stt(s) -> WhisperSTT`. `WhisperSTT(WhisperSTTService)` transcribes with `beam_size=s.stt_beam` and `language="en"`, and drops segments with `no_speech_prob ≥ 0.4` (spike `WhisperBeam`). It uses a process-wide model from `model_cache.whisper(name, threads)` (`WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)`, cached by `(name, threads)`).
  - `make_tts(s) -> TTSService`: every local TTS gets `stop_frame_timeout_s=10.0`. Piper uses `download_dir=MODELS_DIR/piper`. Kokoro is int8 with an onnxruntime session at `intra_op_num_threads=TTS_THREADS`, `inter_op=1`. Cartesia is `CartesiaTTSService(api_key=…, voice_id=s.tts_voice)`; an empty voice raises `ConfigError("TTS_VOICE is required for cartesia")`.
  - `make_llm(s, brief: Brief, system_prompt: str) -> LLMService`: anthropic is `AnthropicLLMService(api_key=…, settings=Settings(model=brief.llm_model or s.llm_model, max_tokens=200, system_instruction=system_prompt, enable_prompt_caching=True))`, with no thinking argument. fake builds `FakeInterviewerLLM(brief)` (Task 10; imported lazily).
  - `make_turn_strategies(s) -> UserTurnStrategies`, plus the VAD: `SileroVADAnalyzer(VADParams(stop_secs=0.2))` and `TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3())`, or `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=SILENCE_S − 0.2)`.
  - An unknown name raises `ConfigError("unknown TTS_PROVIDER 'x' (known: cartesia, kokoro, piper)")`.
  - Provider modules are imported inside `build` so an unselected provider is never imported.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.slow   # constructing Piper may fetch the voice into /models on first run
def test_default_factory_is_local_whisper_and_piper(settings):
    stt, tts = make_stt(settings), make_tts(settings)
    assert isinstance(stt, WhisperSTT) and type(tts).__name__ == "PiperTTSService" and tts._stop_frame_timeout_s == 10.0
    assert provider_info(settings)["stt"] == {"name": "whisper", "model": "base.en", "remote": False}
def test_paid_tts_needs_only_config_and_a_key(monkeypatch):   # acceptance #6
    monkeypatch.setenv("TTS_PROVIDER", "cartesia"); monkeypatch.setenv("TTS_VOICE", "voice-123"); monkeypatch.setenv("CARTESIA_API_KEY", "test-not-a-key")
    s = Settings(); tts = make_tts(s)
    assert type(tts).__name__ == "CartesiaTTSService" and provider_info(s)["tts"]["remote"] is True and required_keys(s) == []
def test_missing_paid_key_is_reported_by_name_only(monkeypatch): # no CARTESIA_API_KEY → required_keys == ["CARTESIA_API_KEY"]
def test_unknown_provider_names_the_known_ones(monkeypatch):
def test_keys_of_unselected_providers_are_not_required(monkeypatch): # default settings, no keys except ANTHROPIC → []
def test_llm_uses_brief_model_and_no_thinking_override(settings, example_brief):
    llm = make_llm(settings, example_brief.model_copy(update={"llm_model": "claude-sonnet-5-5"}), "sys")
    assert llm._settings.model == "claude-sonnet-5-5" and llm._settings.max_tokens == 200
    assert getattr(llm._settings, "thinking", None) in (None, NOT_GIVEN_SENTINEL)   # whatever Pipecat 1.15 uses for "unset"
def test_whisper_model_is_shared(monkeypatch):  # two make_stt calls → same model object (WhisperModel patched to a stub)
```

(The executor checks the real attribute names for `_stop_frame_timeout_s`, `_settings` and the thinking "unset" value against Pipecat 1.15 and adjusts the accessors, not the assertions' meaning.)

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_providers.py`. Expected: FAIL.
- [ ] **Step 3: Implement `providers.py`** (plus `model_cache` inside it).
- [ ] **Step 4: Run the tests to verify they pass.** Expected: 6 passed, 1 skipped (slow); `-m slow` runs it in Task 14.
- [ ] **Step 5: Commit** `src/iassistpro/providers.py tests/test_providers.py`, message `feat: env-driven provider factory with local defaults and Cartesia mapped`.

---

### Task 7: ICE patch with canary, model assets and health status

**Files:**
- Create: `src/iassistpro/ice.py`, `src/iassistpro/assets.py`
- Test: `tests/test_ice.py`, `tests/test_assets.py`

**Interfaces:**
- Produces:
  - `ice.patch_aioice(lo: int, hi: int, host: str) -> None`. It is the spike's `patch_aioice` moved over: it binds `0.0.0.0:<first free port in lo..hi>` and advertises one host candidate `host:<port>`. It is idempotent: `ice.PATCHED` holds the active `(lo, hi, host)`. `ice.EXPECTED_VERSIONS = {"aioice": "0.12.1", "aiortc": "1.15.0"}`.
  - `assets.Asset(id: str, size_mb: int, fetch: Callable[[], None])`, with `required_assets(s: Settings) -> list[Asset]`:
    - `whisper:<model>` (~145 MB for base.en, ~480 for small.en; `faster_whisper.download_model(name)` into `HF_HOME`);
    - `piper:<voice>` (63 MB; piper-tts `download_voices` into `MODELS_DIR/piper`);
    - `kokoro:int8` (~115 MB incl. voices; only when `TTS_PROVIDER=kokoro`; URLs from `spike/common.py`).
    - Silero and smart-turn are bundled, so they have no asset. Remote providers have none.
  - `assets.ModelStatus` with `state: Literal["loading_models", "ready", "error"]`, `done_mb`, `total_mb`, `current`, `error`, and `snapshot() -> dict`.
  - `async assets.prepare(s: Settings, status: ModelStatus, warm: Callable[[], None]) -> None`. It runs each fetch in a thread (`done_mb` advances per asset), then `warm()` (loads Whisper, the TTS voice and the smart-turn session once), then sets `ready`. A failure sets `error = "model_download_failed:<asset id>"` and stops. It is called at app startup in the background.
  - `assets.health(s: Settings, status: ModelStatus, live_session: str | None) -> dict`, the `Health` JSON. A non-empty `required_keys(s)` gives `status "error"` and `error "missing_key:<VAR>"` (first missing key), whatever the download state.

- [ ] **Step 1: Write the failing tests**

```python
# test_ice.py — canary on the patched internals (AS4)
def test_pinned_versions(): assert (aioice.__version__, aiortc.__version__) == ("0.12.1", "1.15.0")
def test_patched_internals_still_exist():
    assert list(inspect.signature(ice.Connection.get_component_candidates).parameters) == ["self", "component", "addresses", "timeout"]
    assert hasattr(ice, "StunProtocol") and "_protocols" in vars(ice.Connection(ice_controlling=True))
    assert {"foundation", "component", "transport", "priority", "host", "port", "type"} <= set(inspect.signature(Candidate).parameters)
async def test_patch_binds_inside_range_and_advertises_host():
    patch_aioice(40100, 40101, "127.0.0.1")
    a, b, c = (ice.Connection(ice_controlling=True) for _ in range(3))
    await a.gather_candidates(); await b.gather_candidates()
    assert {(x.host, x.port) for x in a.local_candidates + b.local_candidates} == {("127.0.0.1", 40100), ("127.0.0.1", 40101)}
    with pytest.raises(OSError, match="no free UDP port"): await c.gather_candidates()
    await a.close(); await b.close()
# test_assets.py
async def test_progress_then_ready(settings): # stub fetches; snapshots go loading 0/208 → 145/208 → 208/208 → ready
async def test_download_failure_is_reported(settings): # piper fetch raises → error "model_download_failed:piper:en_US-lessac-medium"
def test_missing_key_is_an_error_state(monkeypatch):   # no ANTHROPIC_API_KEY → status "error", error "missing_key:ANTHROPIC_API_KEY"
def test_fake_llm_needs_no_key(monkeypatch):           # LLM_PROVIDER=fake → no key error
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_ice.py tests/test_assets.py`. Expected: FAIL.
- [ ] **Step 3: Implement `ice.py` and `assets.py`.**
- [ ] **Step 4: Run the tests to verify they pass.** Expected: 8 passed.
- [ ] **Step 5: Commit** the four files, message `feat: aioice port-range patch with canary; model assets and health status`.

---

### Task 8: Interviewer prompt, tools and the director

**Files:**
- Create: `src/iassistpro/prompt.py`, `src/iassistpro/director.py`
- Test: `tests/test_prompt.py`, `tests/test_director.py`

**Interfaces:**
- Consumes: `Brief` (Task 2); `CostMeter`, `LimitTracker`, notes and closings (Task 5).
- Produces:
  - `prompt.build_system_prompt(brief: Brief) -> str`. It is deterministic: no timestamps, no ids beyond the question ids. It contains role, company, role summary, the candidate's first name and summary, and `planned_questions` as `id: text` lines, each with its `probe_for` marked "for your follow-up decisions only; never say these". It also carries `max_minutes` and `max_follow_ups`, plus the §5.4 rules:
    - spoken plain sentences, no lists or markdown, one question per turn, under 30 words;
    - **open every turn with a short clause** ("Thanks. …");
    - greet by naming role and company and the rough length, then ask the first question;
    - follow up only when the answer is vague, makes a claim worth probing or skips half the question, and refer to what they said;
    - never give feedback, scores or hints, never reveal the notes, stay in character;
    - repeat on request; on "skip this one" move on;
    - **speak first, then call the tool in the same reply:** `ask_question` before a main question, `follow_up` before a follow-up, `end_interview` with the closing sentence.
  - `prompt.TOOLS(brief) -> ToolsSchema` with three `FunctionSchema`s from `pipecat.adapters.llm.schemas`:
    - `ask_question{question_id: enum planned ids}`;
    - `follow_up{question_id: enum planned ids}`;
    - `end_interview{reason: enum ["completed", "candidate_request", "time_limit", "budget"]}`.
  - `prompt.JOIN_MESSAGE = "(The candidate has joined the call.)"`.
  - `director.InterviewState(brief, meter: CostMeter, tracker: LimitTracker)` (pure) with:
    - `current_question_id`, `current_kind` (`"greeting"` until the first tool), `question_number`, `ending: bool`, `end_reason: EndReason | None`;
    - `on_tool(name: str, args: dict, response_had_text: bool) -> ToolOutcome(run_llm: bool, result: dict)`;
    - `notes_due(elapsed_s: float) -> list[str]` (each note at most once; already prefixed);
    - `hard_stop_due(elapsed_s: float) -> Literal["time_limit", "budget"] | None`;
    - `request_end() -> None`, `on_disconnect() -> None`, `on_error() -> None`, `final_end_reason() -> EndReason`.
  - Rules in `on_tool`:
    - `run_llm = not response_had_text`.
    - An unknown `question_id` keeps the current question, returns `{"ok": false, "error": "unknown_question_id"}` and is logged as a count.
    - `end_interview` sets `ending`. Its reason maps `candidate_request` → `ended_by_candidate` and others to themselves. A wrap-up already triggered by time or budget wins over the model's `completed`.
    - Precedence for the final reason: `request_end` (ended_by_candidate) > hard stop > wrap-up reason > tool reason. A disconnect while not `ending` gives `disconnected`, and a pipeline exception gives `error`.
  - `director.InterviewDirector(FrameProcessor)`, placed between the user aggregator and the LLM. It registers the three handlers on the LLM. Its `on_frame` is an `async` callback for the transcript and session (Task 9 and 11 hooks).
    - On each `LLMContextFrame` going downstream: if `hard_stop_due`, it drops the frame, pushes `TTSSpeakFrame(CLOSE_TIME|CLOSE_BUDGET)` and marks ending. Otherwise it appends `notes_due` to the context as one extra text block on the last user message, then passes the frame on.
    - On `request_end()`: it interrupts the current reply, pushes `TTSSpeakFrame(CLOSE_ENDED)` and marks ending.
    - Once ending and the closing's `BotStoppedSpeakingFrame` has passed (or 15 s), it pushes `EndTaskFrame` upstream.
    - A 1 s timer task also checks `hard_stop_due` while nobody speaks.
  - `director.SpokenTextProbe(BaseObserver)` records whether the current LLM response pushed a non-blank `LLMTextFrame` before its function call. It resets on `LLMFullResponseStartFrame`.

- [ ] **Step 1: Write the failing tests**

```python
# test_prompt.py
def test_prompt_lists_planned_questions_in_order_with_probes(example_brief): # q1..q6 ids/text in order; each probe present; max_main_questions=3 → q4 absent
def test_prompt_rules_present(example_brief):  # "short clause", "never give feedback", "speak first, then call", "under 30 words", role+company
def test_prompt_is_deterministic(example_brief): assert build_system_prompt(example_brief) == build_system_prompt(example_brief.model_copy())
def test_tool_enums_are_the_planned_ids(example_brief):
# test_director.py
def test_tool_call_with_text_does_not_rerun_llm(state):
    assert state.on_tool("ask_question", {"question_id": "q1"}, response_had_text=True).run_llm is False
    assert (state.current_question_id, state.current_kind, state.question_number) == ("q1", "main", 1)
def test_tool_call_without_text_triggers_one_llm_run(state):
    assert state.on_tool("ask_question", {"question_id": "q1"}, response_had_text=False).run_llm is True
def test_follow_up_counts_and_note_once(state):   # max_follow_ups=2: after 2 follow_up(q2), notes_due has NOTE_NO_MORE_FOLLOW_UPS(q2) once
def test_last_main_note(state):                   # after asking all planned ids → NOTE_LAST_MAIN once
def test_time_wrap_then_hard_stop(state):         # notes_due(1500) has NOTE_TIME_UP; hard_stop_due(1590) == "time_limit"
def test_budget_hard_stop_before_next_run(state): # meter pushed past projection → hard_stop_due(10) == "budget"
@pytest.mark.parametrize("events, expected", [
    (["end:completed"], "completed"), (["end:candidate_request"], "ended_by_candidate"),
    (["time_wrap", "end:completed"], "time_limit"), (["request_end", "end:completed"], "ended_by_candidate"),
    (["disconnect"], "disconnected"), (["end:completed", "disconnect"], "completed"), (["error"], "error")])
def test_end_reason_precedence(state, events, expected): ...
def test_unknown_question_id_keeps_current(state):
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_prompt.py tests/test_director.py`. Expected: FAIL.
- [ ] **Step 3: Implement `prompt.py` and `director.py`.** The note channel is an extra text block on the last user message. (The executor confirms that Pipecat 1.15's Anthropic adapter keeps a multi-block user message; if it doesn't, it appends the note text to that message's text instead.)
- [ ] **Step 4: Run the tests to verify they pass.** Expected: all pass.
- [ ] **Step 5: Commit** the four files, message `feat: interviewer prompt, tool schemas and limit-enforcing director`.

---

### Task 9: Transcript, latency and recorder

**Files:**
- Create: `src/iassistpro/transcript.py`, `src/iassistpro/recorder.py`
- Test: `tests/test_transcript.py`, `tests/test_recorder.py`

**Interfaces:**
- Consumes: `Turn`, `Latency`, `AudioInfo` (Task 2); `InterviewState.current_question_id`/`current_kind` (Task 8).
- Produces:
  - `transcript.TranscriptBuilder(on_turn: Callable[[Turn], None], vad_stop_s: float = 0.2)`. `on_frame(frame, t_ms: int)` is pure and also called by `TranscriptObserver(BaseObserver)` (deduped by `frame.id`, t from session start = client connect). `set_label(question_id: str | None, kind: str)` is called by the director when a tool is handled or a fixed closing starts. It exposes `turns: list[Turn]`, `reply_gaps_ms: list[int]` and `latency() -> Latency`.
  - Candidate turn: opens at `UserStartedSpeakingFrame`; its text is the `TranscriptionFrame`s joined with spaces; it closes at `UserStoppedSpeakingFrame`. `start_ms` is the first VAD start, `end_ms` the last `VADUserStoppedSpeakingFrame.timestamp − stop_secs`. Blank text means no turn. It takes the interviewer's latest `question_id`/`kind`.
  - Interviewer turn: one per LLM response or `TTSSpeakFrame`. Its text is the `TTSTextFrame`s actually spoken (an interruption keeps the words spoken so far). `start_ms` is the first `BotStartedSpeakingFrame`, and it closes at the `BotStoppedSpeakingFrame` after the response ended. Its label is the one set during that response (greeting if none yet).
  - Reply gap = `BotStartedSpeaking t` − the preceding candidate turn's `end_ms`, counted only if < 12 000 ms. `latency()` gives `n`, the median and `p90 = sorted[max(0, round(0.9·n) − 1)]` (spike rule), as ints.
  - `recorder.AudioSink(raw_path: Path)` is attached to `AudioBufferProcessor(sample_rate=16000, num_channels=2, buffer_size=1 MiB)`. It appends each `on_audio_data` chunk (stereo PCM16) to `audio.raw` and `close()`s on `stop_recording()`.
  - `async recorder.encode(raw_path: Path, out_path: Path, keep_wav: bool) -> AudioInfo` mixes down to mono 16 kHz and writes `soundfile.write(out, mono, 16000, format="OGG", subtype="OPUS")` in `asyncio.to_thread`. It deletes `audio.raw` unless `keep_wav` (then writes `audio.stereo.wav`). An empty or missing raw gives `state "none"`, and an exception gives `state "failed"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_turns_labels_and_offsets():   # synthetic frame script: greeting response + ask q1, candidate answer, follow_up q1 response, answer
    assert [(t.speaker, t.kind, t.question_id) for t in b.turns] == [
        ("interviewer", "main", "q1"), ("candidate", "main", "q1"), ("interviewer", "follow_up", "q1"), ("candidate", "follow_up", "q1")]
    assert b.turns[1].text == "I led the team. We shipped." and b.turns[1].end_ms == 9_800   # VAD stop at 10.0 s − 0.2
def test_greeting_without_tool_is_greeting(): ...
def test_interrupted_reply_keeps_spoken_words_only(): ...
def test_blank_transcription_makes_no_turn(): ...
def test_latency_median_and_p90():     # gaps [3000, 3500, 3900, 4000, 4100, 4200, 4300, 4400, 4500, 9000] → median 4150, p90 4500
def test_on_turn_called_once_per_closed_turn():
async def test_encode_writes_small_mono_ogg_off_the_loop(tmp_path):
    raw = write_stereo_sine(tmp_path / "audio.raw", seconds=3)
    ticks = 0
    async def ticker():
        nonlocal ticks
        while True: ticks += 1; await asyncio.sleep(0.01)
    t = asyncio.create_task(ticker()); info = await encode(raw, tmp_path / "audio.ogg", keep_wav=False); t.cancel()
    assert info.state == "ready" and info.format == "ogg_opus" and abs(info.duration_ms - 3000) <= 50
    assert sf.info(tmp_path / "audio.ogg").channels == 1 and not raw.exists() and ticks > 5
async def test_encode_empty_raw_is_none(tmp_path):
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_transcript.py tests/test_recorder.py`. Expected: FAIL.
- [ ] **Step 3: Implement both modules.**
- [ ] **Step 4: Run the tests to verify they pass.** Expected: all pass.
- [ ] **Step 5: Commit** the four files, message `feat: transcript builder with question labels and reply latency; Ogg/Opus recorder`.

---

### Task 10: Pipeline assembly, scripted fake LLM and the pipeline test

**Files:**
- Create: `src/iassistpro/pipeline.py`, `src/iassistpro/fake_llm.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Tasks 2–9 (`make_*`, `InterviewDirector`, `InterviewState`, `SpokenTextProbe`, `TranscriptBuilder`/`TranscriptObserver`, `AudioSink`, `encode`, `CostMeter`, `SessionStore`, `log_event`).
- Produces:
  - `pipeline.SessionHooks` (Protocol, implemented by Task 11's `LiveSession`): `brief: Brief`, `session_id: str`, `store: SessionStore`, `end_requested: asyncio.Event`, `on_live()`, `on_progress(qid, kind, number)`, `on_end(reason: EndReason, error: str | None)`, `on_audio(info: AudioInfo)`.
  - `async pipeline.run_interview(hooks: SessionHooks, transport: BaseTransport, s: Settings, *, stt=None, tts=None, llm=None) -> None`. It wires `transport.input() → stt → user_aggregator → director → llm → tts → transport.output() → recorder → assistant_aggregator` with `LLMContext(messages=[{"role": "user", "content": JOIN_MESSAGE}], tools=TOOLS(brief))`. Params are `PipelineParams(enable_metrics=True, enable_usage_metrics=True, audio_in_sample_rate=16000, audio_out_sample_rate=24000)`, and Pipecat's idle-timeout cancel is turned off (the director owns time).
    - On client connect: `on_live`, start recording, queue `LLMRunFrame`.
    - On each `MetricsFrame` with `LLMUsageMetricsData`: `meter.add` (mapping `prompt_tokens` → `input_tokens`, `cache_read_input_tokens` → `cached_input_tokens`, `cache_creation_input_tokens` → `cache_write_tokens`, `completion_tokens` → `output_tokens`), then `store.write_usage(meter.rows())` (AD4). TTS/STT usage metrics count only when that provider is `remote`.
    - Each closed turn goes to `store.append_turn` and `log_event("turn_closed", speaker=…, chars=len, ms=…)`.
    - On disconnect: `state.on_disconnect()` and cancel the task.
    - On exit: `on_end(state.final_end_reason(), error)`, then `encode` and `on_audio`. Exceptions are logged by type name only.
  - `fake_llm.FakeInterviewerLLM(brief: Brief, *, tool_first_no_text: bool = False)` (an `LLMService` subclass). On each `LLMContextFrame` it emits `LLMFullResponseStartFrame`, the `LLMTextFrame`s, then calls the tool through the real `run_function_calls([FunctionCallFromLLM(...)])`, then `LLMFullResponseEndFrame`, then `MetricsFrame(LLMUsageMetricsData(LLMTokenUsage(prompt_tokens=200, completion_tokens=40, cache_read_input_tokens=1000 (0 on run 1), cache_creation_input_tokens=1000 on run 1 else 0)))`. Its script (k = candidate turns in context):
    - k = 0: `"Hello {name}, thanks for joining. This is a mock interview for the {title} role at {company}, about {max_minutes} minutes. {q1.text}"` + `ask_question(q1)`.
    - Any note containing "Close the interview" or "Time is up": `"Thank you, {name}. That's the end of the interview."` + `end_interview(budget|time_limit)`.
    - After the first answer to planned question 2, once: `"Thanks. Can you say more about your own part in that?"` + `follow_up(q2)`.
    - Otherwise the next planned question as `"Thanks. {text}"` + `ask_question`, and after the last answer the closing + `end_interview(completed)`.
    - With `tool_first_no_text`, run 1 emits only the tool call. The `run_llm=True` re-run then speaks the run-1 text.

- [ ] **Step 1: Write the failing tests** (Pipecat `pipecat.tests.utils.run_test` or a `PipelineTask` driven in-process; no audio, no network). The user side is fed as `UserStartedSpeakingFrame` → `TranscriptionFrame` → `UserStoppedSpeakingFrame` (with `VADUserStoppedSpeakingFrame` first). If the aggregator needs audio-driven VAD, the executor uses Pipecat's external user-turn strategy for the test instead; the assertions stay. A `FakeTTS` emits `TTSStartedFrame`/`TTSTextFrame`/`TTSStoppedFrame`, and a stub output emits `BotStarted`/`BotStoppedSpeakingFrame`.

```python
async def test_scripted_session_produces_turns_ids_and_end_reason(tmp_store, brief_3q):
    hooks = await run_scripted(brief_3q, answers=["a1", "a2", "a2 more", "a3"])
    labels = [(t.speaker, t.kind, t.question_id) for t in tmp_store.result(hooks.session_id).turns]
    assert labels == [("interviewer","main","q1"),("candidate","main","q1"),("interviewer","main","q2"),("candidate","main","q2"),
                      ("interviewer","follow_up","q2"),("candidate","follow_up","q2"),("interviewer","main","q3"),("candidate","main","q3"),
                      ("interviewer","closing",None)]   # closing turns always carry question_id None (set_label(None, "closing"))
    assert hooks.end_reason == "completed"
async def test_usage_is_persisted_after_every_llm_turn(tmp_store, brief_3q):  # write_usage spy called 5 times; final row sums all 5 runs
async def test_scripted_session_tool_first_still_speaks(tmp_store, brief_3q):  # tool_first_no_text → first interviewer turn has the greeting text; exactly one extra LLM run
async def test_budget_cap_ends_with_fixed_closing_and_no_extra_llm_call(tmp_store, brief_3q):  # cap tiny → end_reason budget, last turn text == CLOSE_BUDGET.format(name="Sam"), LLM runs ≤ 2
async def test_end_request_mid_session(tmp_store, brief_3q):    # set end_requested after answer 1 → ended_by_candidate, closing CLOSE_ENDED
async def test_disconnect_ends_disconnected_with_partial_transcript(tmp_store, brief_3q):
async def test_full_session_logs_no_content(tmp_store, captured_logs):
    brief = canary_brief()            # CANARY in role/candidate summaries, question texts and probe_for
    await run_scripted(brief, answers=[f"my answer {CANARY}"] * 4)
    assert CANARY not in captured_logs() and "turn_closed" in captured_logs()
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_pipeline.py`. Expected: FAIL.
- [ ] **Step 3: Implement `pipeline.py` and `fake_llm.py`.**
- [ ] **Step 4: Run the tests to verify they pass, then the whole suite.** `./scripts/dtest.sh pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** the three files, message `feat: interview pipeline with per-turn usage, scripted fake LLM and pipeline tests`.

---

### Task 11: HTTP API and session manager

**Files:**
- Create: `src/iassistpro/manager.py`, `src/iassistpro/api.py`, `src/iassistpro/__main__.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: everything above; `pipeline.run_interview` as the default runner.
- Produces:
  - `manager.LiveSession` implements `SessionHooks` and holds the in-memory `token_hash` (sha256), `expires_at`, `pc_id`, `created_mono`, `connected_mono` and `status()`.
  - `manager.SessionManager(s: Settings, store: SessionStore, status: ModelStatus, runner: Callable[[LiveSession, BaseTransport, Settings], Awaitable[None]] = run_interview, clock=time.monotonic)` with:
    - `async create(brief: Brief) -> dict` (`{session_id, connect}`), which raises `Busy(session_id)` or `NotReady(status)`; the check and the reservation sit under one `asyncio.Lock`;
    - `async offer(session_id, token, body: dict) -> dict`;
    - `async patch(session_id, token, body: dict) -> dict`;
    - `async websocket(session_id, token, ws: WebSocket) -> None`;
    - `status(session_id) -> dict`, `async end(session_id) -> dict`, `result(session_id) -> Result`, `audio_path(session_id) -> Path`, `async delete(session_id) -> None`;
    - `live_session_id: str | None`, and `async housekeeping()` (every 10 s: expire waiting sessions past `expires_at` → `failed`/`not_connected`; every 10 min: `store.sweep`).
    - Startup runs `store.recover_on_startup`.
    - The lock is held while a session is `waiting` or `live` and freed on any terminal state.
    - `end` on `waiting` → `ended`, `ended_by_candidate`, no turns, audio `none`.
    - `offer` builds `SmallWebRTCConnection` through one `SmallWebRTCRequestHandler` and runs the runner in a background task. A second offer → `WrongState`.
  - Exceptions map to the codes and statuses in "Service interface".
  - `api.create_app(s: Settings | None = None, manager: SessionManager | None = None) -> FastAPI`, with the routes from "Service interface". Startup calls `setup_logging`, `ice.patch_aioice(*s.ice_range, s.ice_host)` when `TRANSPORT=smallwebrtc`, `assets.prepare` in the background and `manager.housekeeping`. A custom 422 handler returns `{"detail": {"code": "invalid_brief", "message": <first error's loc + msg>, "errors": <pydantic errors without "input">}}` and logs only `log_event("invalid_brief", n_errors=…)`. A request middleware logs method, route template, status and ms.
  - `python -m iassistpro` runs uvicorn on `0.0.0.0:$PORT` with `access_log=False`.

- [ ] **Step 1: Write the failing tests** (httpx `AsyncClient` over `create_app` with a `FakeRunner`. The fake waits on `end_requested` or scripted events, appends two turns, writes usage and calls `on_end`. `offer` uses a stubbed request handler that returns a canned answer and calls the connection callback.)

```python
async def test_health_loading_then_ready(client, status): ...
async def test_create_returns_connect_info(client):     # 201; transport smallwebrtc; offer_path; token len ≥ 40; expires_at ≈ now+120 s
async def test_not_ready_is_503(client_loading):
async def test_second_session_while_waiting_is_busy(client):   # 409 code busy + session_id
async def test_offer_token_is_single_use(client):        # first offer 200 → state live; second 409 wrong_state; PATCH with same token 200
async def test_wrong_token_is_401_and_does_not_change_state(client):
async def test_unused_session_expires_and_frees_lock(client, clock):  # advance 121 s; housekeeping → failed/not_connected; offer → 410; new create 201
async def test_end_on_live_then_result_and_audio_flow(client):  # end → 202; status ended; result 200 validates against JSON Schema; audio 409 until on_audio ready, then 200 audio/ogg
async def test_result_before_end_is_409(client):
async def test_disconnect_mid_session_keeps_turns_and_frees_lock(client):  # runner calls on_end("disconnected") → result has 2 turns + usage; new create 201
async def test_delete_removes_folder_and_live_delete_is_409(client, tmp_path):
async def test_invalid_brief_422_logs_no_content(client, captured_logs):  # CANARY + email in summary → 422 invalid_brief; CANARY not in logs
async def test_dev_client_only_when_enabled(client): # 404 by default
async def test_unknown_session_404(client):
```

- [ ] **Step 2: Run the tests to verify they fail.** `./scripts/dtest.sh pytest -q tests/test_api.py`. Expected: FAIL.
- [ ] **Step 3: Implement `manager.py`, `api.py`, `__main__.py`.**
- [ ] **Step 4: Run the tests to verify they pass, then the full suite.** `./scripts/dtest.sh pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** the four files, message `feat: HTTP API with one-session lock, one-time connect tokens and result/audio endpoints`.

---

### Task 12: Runtime image, standalone compose, dev client and README

**Files:**
- Modify: `docker/Dockerfile` (stage `runtime`), `docker/compose.yml` (service `interviewer`)
- Create: `src/iassistpro/dev_client/client.html`, `scripts/up-live.sh`, `README.md` (replace the stub)

**Interfaces:**
- Produces:
  - Stage `runtime` copies `src/` and `contracts/` and creates user `app` (uid 10001) owning `/models` and `/data/sessions`. It exposes 8000. Its healthcheck is `python -c "import urllib.request,json,sys; sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'))['status']!='error' else 1)"` (interval 15 s), and its CMD is `python -m iassistpro`.
  - Service `interviewer`:
    - `build: {context: .., dockerfile: docker/Dockerfile, target: runtime}`, no `image:`;
    - ports `127.0.0.1:8770:8000` and `127.0.0.1:40020-40022:40020-40022/udp`;
    - `environment:` `ICE_PORTS=40020-40022`, `ICE_HOST=127.0.0.1`, `DEV_CLIENT=${DEV_CLIENT:-1}`, `LLM_PROVIDER=${LLM_PROVIDER:-anthropic}`, `TTS_PROVIDER=${TTS_PROVIDER:-piper}`, `ANTHROPIC_API_KEY` (by name, no value);
    - volumes `interviewer_models:/models`, `interviewer_sessions:/data/sessions`; `restart: "no"`.
  - `dev_client/client.html` is the spike's `client.html` reworked. Its query is `?session=<id>&token=<t>&dur=<s, optional>`. It connects with `webrtcRequestParams: {endpoint: "/sessions/<id>/offer", headers: {"X-Session-Token": t}}` (falling back to `?token=` in the endpoint if the 1.11.2 transport drops headers). It keeps the spike's 10 ms energy meters (mic vs bot) and gap pairing. It exposes `window.iassist = {events, gaps, firstBotAudioMs, captions: {user: n, bot: n}, done}`, with caption counts from `onUserTranscript`/`onBotTranscript` (counts only), and a red "Recording" dot. It disconnects after `dur` or when the server ends.
  - `scripts/up-live.sh` (Git Bash): `set -euo pipefail; set +x`. It reads the `ANTHROPIC_API_KEY=` line of `${JOBLENS_ENV:-C:/2026/rkumar/JobLens/.env}` into an exported variable without echoing it, then runs `LLM_PROVIDER=anthropic docker compose -f docker/compose.yml up -d --build interviewer`. Its only output is "interviewer starting (key passed by name)".
  - README sections:
    - what it is (generic; any app that can send a brief);
    - quick start: `docker compose -f docker/compose.yml up -d --build interviewer`, wait for `/health` `ready` ("downloads ~210 MB of models once into the `interviewer_models` volume"), `curl -s -X POST localhost:8770/sessions -H 'content-type: application/json' -d @contracts/examples/brief.example.json`, open `http://127.0.0.1:8770/dev/client?session=…&token=…` within 2 minutes, with headphones;
    - the full "Service interface" (endpoints, connect, health states, env table, ports per stack incl. prod/dev from AS4, 1:1 UDP rule);
    - providers and how to add a paid one (one `REGISTRY` entry + key; Cartesia as the worked example);
    - privacy (no content in logs, temp results deleted on DELETE or after 24 h, a paid STT sends the voice to the provider);
    - tests (`dtest.sh`, `-m slow`, e2e free and live with `IASSIST_LIVE=1` and the spend cap);
    - known limits (one session at a time; CPU latency ~4 s; WebSocket fallback).

- [ ] **Step 1: Write the failing check.** `curl -s 127.0.0.1:8770/health` returns nothing (connection refused).
- [ ] **Step 2: Implement the runtime stage, compose service, dev client, `up-live.sh` and README.**
- [ ] **Step 3: Build and start free (fake LLM), and wait for ready**

Run: `LLM_PROVIDER=fake docker compose -f docker/compose.yml up -d --build interviewer`, then poll `curl -s 127.0.0.1:8770/health` every 5 s for up to 10 min.
Expected: `loading_models` with growing `done_mb`, then `"status":"ready"`, `providers.stt.model "base.en"`, `tts.name "piper"`, `transport "smallwebrtc"`. `docker compose -f docker/compose.yml ps` shows `healthy`. `docker logs iassistpro-interviewer-1` has no transcript or brief text (it is grepped for "Northwind" and "Sam": 0 hits).

- [ ] **Step 4: Manual session smoke.** POST the example brief, then open the dev client URL once in Edge. Expected: the greeting is heard within 10 s. Press nothing; after 2 minutes POST `/end`. Expected: `GET /result` validates and `GET /audio` returns 200 within ~1 min. Then DELETE.
- [ ] **Step 5: Commit** `docker/Dockerfile docker/compose.yml src/iassistpro/dev_client/client.html scripts/up-live.sh README.md`, message `feat: runtime image, standalone compose with models volume, dev client and README`.

---

### Task 13: Spoken end-to-end test (Playwright fake mic)

**Files:**
- Create: `scripts/gen_candidate_clips.py`, `tests/audio/candidate/answers.json`, `tests/audio/candidate/a00.wav` … `a11.wav` (16 kHz mono PCM16, committed), `scripts/make_session_wav.py`, `e2e/package.json`, `e2e/package-lock.json`, `e2e/playwright.config.ts`, `e2e/lib/harness.ts`, `e2e/fixtures/brief.e2e.json`, `e2e/fixtures/brief.live.json`, `e2e/spoken.e2e.ts`, `tests/test_e2e_fixtures.py`

**Interfaces:**
- Consumes: the running standalone service (Task 12); the contract schemas; `window.iassist` (Task 12).
- Produces:
  - `answers.json` holds the spike's 12 `ANSWERS` (`common.py`) as `[{"id": "a00", "text": …}, …]`. `gen_candidate_clips.py` (run in the dev container) voices them with Kokoro `am_michael` and writes `aNN.wav` at 16 kHz.
  - `make_session_wav.py --clips a00,a01,… --lead 25 --gap 14 --out e2e/out/<name>.wav` writes 48 kHz mono PCM16 plus `<name>.schedule.json` (soxr resample, ×0.8 gain, as the spike).
  - `brief.e2e.json`: Sam / Northwind, 5 questions, `max_follow_ups 1`, `max_minutes 8`, `max_llm_cost_usd 0.05`. With the fake script, the clips are a00 (q1), a01 (q2), a02 (follow-up q2), a04 (q3), a05 (q4), a06 (q5).
  - `brief.live.json`: 8 questions, `max_minutes 7`, `max_llm_cost_usd 0.05`, `llm_model "claude-haiku-5-5"`, Haiku prices.
  - `harness.ts` exports:
    - `runSession({brief, wav, durS, channel, endAfterS?}) -> {result, gaps, firstBotAudioMs, captions, createMs}`. It POSTs `/sessions`, launches Chromium with the spike's fake-media flags (`--use-file-for-fake-audio-capture=<abs wav>%noloop`), opens `/dev/client?session&token&dur` and waits for `iassist.done`, optionally POSTing `/end`. It then polls `/sessions/{id}` to `ended`, GETs the result, polls the audio up to 120 s, and finally DELETEs.
    - `costUsd(result, prices)` (same formula as Task 2) and `p90(xs)` (spike rule).
  - `E2E_BASE` defaults to `http://127.0.0.1:8770`; `ajv` validates the result against `contracts/result.v1.schema.json`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_e2e_fixtures.py
def test_committed_clips_are_16k_mono_and_match_answers():  # 12 files; sf.info samplerate 16000, channels 1; ids match answers.json; total < 4 MB
def test_e2e_briefs_validate():                             # Brief.model_validate for both fixtures; no contact details
```

```ts
// e2e/spoken.e2e.ts
test("spoken session with the scripted interviewer (free)", async () => {
  const r = await runSession({ brief: e2eBrief, wav: SESSION_FAKE, durS: 150, channel: "chromium" });
  expect(r.result.end_reason).toBe("completed");
  expect(r.result.turns.filter(t => t.speaker === "candidate").map(t => [t.question_id, t.kind]))
    .toEqual([["q1","main"],["q2","main"],["q2","follow_up"],["q3","main"],["q4","main"],["q5","main"]]);
  expect(wer(candidateText(r.result), scriptedText)).toBeLessThan(0.15);
  expect(r.result.audio.state).toBe("ready");                              // after polling
  expect(r.result.latency.n).toBeGreaterThanOrEqual(5); expect(r.gaps.length).toBeGreaterThanOrEqual(5);
  expect(r.captions.user).toBeGreaterThan(0); expect(r.captions.bot).toBeGreaterThan(0);
  expect(r.createMs + r.firstBotAudioMs).toBeLessThan(10_000);             // acceptance #1 (models warm)
  expect(validateResult(r.result)).toBe(true);
});
test("live session with Claude Haiku (paid, opt-in)", async () => {
  test.skip(process.env.IASSIST_LIVE !== "1", "set IASSIST_LIVE=1 to spend (cap $" + MAX_SPEND + ")");
  test.skip((await health()).providers.llm.name !== "anthropic", "service not started with scripts/up-live.sh");
  const r = await runSession({ brief: liveBrief, wav: SESSION_LIVE, durS: 300, channel: process.env.E2E_CHANNEL ?? "chromium", endAfterS: 300 });
  const cost = costUsd(r.result, liveBrief.llm_prices_per_mtok); console.log(`live spend $${cost.toFixed(4)}`);
  expect(cost).toBeLessThanOrEqual(MAX_SPEND);                              // E2E_MAX_SPEND_USD, default 0.10
  expect(["completed", "ended_by_candidate", "time_limit"]).toContain(r.result.end_reason);
  expect(r.gaps.length).toBeGreaterThanOrEqual(10);
  writeFileSync(`out/live-${Date.now()}.json`, JSON.stringify({ gaps: r.gaps, result_latency: r.result.latency, usage: r.result.usage, cost }));
});
```

`wer` and `candidateText` are in `harness.ts` (the spike's normaliser and edit distance, ported).

- [ ] **Step 2: Generate the clips and session WAVs, and run the fixture test to verify it fails before the clips exist**

Run: `./scripts/dtest.sh pytest -q tests/test_e2e_fixtures.py` → FAIL (no clips). Then `./scripts/dtest.sh python scripts/gen_candidate_clips.py`, `./scripts/dtest.sh python scripts/make_session_wav.py --clips a00,a01,a02,a04,a05,a06 --lead 25 --gap 14 --out e2e/out/session_fake.wav`, and the same with all 12 clips `--out e2e/out/session_live.wav`.

- [ ] **Step 3: Implement the e2e package** (`cd e2e && npm install` with exact versions `@playwright/test 1.56.1`, `ajv 8.17.1`; `npx playwright install chromium` if not cached).

- [ ] **Step 4: Run the free e2e against the fake-LLM service**

Run: `./scripts/dtest.sh pytest -q tests/test_e2e_fixtures.py`, then (service from Task 12 Step 3 up with `LLM_PROVIDER=fake`) `cd e2e && npx playwright test spoken.e2e.ts`.
Expected: fixtures 2 passed; the free test passes and the live one is skipped with the cap message. Also run `-g "free" --project` once with `E2E_CHANNEL=msedge` (Edge 156). Expected: pass.

- [ ] **Step 5: Commit** the scripts, `tests/audio/candidate/`, `tests/test_e2e_fixtures.py` and `e2e/` (not `e2e/out`, not `node_modules`), message `test: spoken end-to-end session through the real transport with a fake microphone`.

---

### Task 14: Latency acceptance run (AS3), decisions log, full verification

**Files:**
- Create: `scripts/latency_report.py`, `docs/latency-acceptance.md`
- Modify: `docs/rupak_decisions.md` (add the table "Builder's calls during implementation (release 2a, IAssistPro)")

**Interfaces:**
- Consumes: `e2e/out/live-*.json` (Task 13).
- Produces: `latency_report.py [files…]` prints, for each run, `n`, the browser median/p90, the server median/p90 (from `result_latency`), `cache_read_input_tokens` total and the cost, plus `PASS`/`FAIL` against **median ≤ 4000 ms and p90 ≤ 5000 ms** (browser, ≥ 10 gaps).

- [ ] **Step 1: Start the service with the key, without printing it.** `docker compose -f docker/compose.yml down` (IAssistPro only), then `./scripts/up-live.sh`. Wait for `/health` `ready` with `llm.name "anthropic"` and `llm.model "claude-haiku-5-5"`. Defaults: Piper + base.en + smart-turn, SmallWebRTC. Note the Windows power mode and whether the laptop is plugged in (AS3 hint); don't change system settings.
- [ ] **Step 2: Run the acceptance session while sampling CPU.** In one shell: `while true; do docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' iassistpro-interviewer-1 >> e2e/out/stats.txt; sleep 2; done` (our container only; stopped afterwards). In another: `cd e2e && IASSIST_LIVE=1 E2E_MAX_SPEND_USD=0.10 npx playwright test -g "live"`. Expected: PASS and "live spend $0.00xx" printed.
- [ ] **Step 3: Evaluate.** Run `./scripts/dtest.sh python scripts/latency_report.py e2e/out/live-*.json`. Expected: `PASS` with median ≤ 4000 and p90 ≤ 5000. If it fails, run **one** more session (same config; the laptop plugged in if possible) and report both runs. Neither acceptance nor defaults change; a miss goes to the architect with the breakdown. Total live spend stays under $0.30.
- [ ] **Step 4: Write `docs/latency-acceptance.md`:**
  - versions (Pipecat, aiortc/aioice, faster-whisper, piper, image digest);
  - config;
  - the table per run (n, browser median/p90, server median/p90, connect → first audio, cache-read tokens, cost);
  - CPU average/peak and memory from `stats.txt`;
  - the verdict against AS3 and acceptance #1;
  - notes: synthetic voice only; the AS5 re-check on Rupak's own voice happens in the 2b live dev session, local only, never committed.
  It contains no transcript text.
- [ ] **Step 5: Stop the service.** `docker compose -f docker/compose.yml down` (keeps volumes). Then confirm `docker ps` shows no `iassistpro-*` and the JobLens containers are unchanged.
- [ ] **Step 6: Full verification.** `./scripts/dtest.sh pytest -q` (all pass), `./scripts/dtest.sh pytest -q -m slow` (all pass), `git status` (only intended files), and `git grep -nI -E "rupak|@avantalabs|sk-ant-" -- . ':!docs'` (no hits outside docs).
- [ ] **Step 7: Decisions log.** Append the builder's calls, starting with the guesses below (as confirmed or changed by the architect) and any new ones, plus the measured latency and live spend.
- [ ] **Step 8: Commit** `scripts/latency_report.py docs/latency-acceptance.md docs/rupak_decisions.md`, message `docs: latency acceptance run and builder's calls for release 2a`. Don't tag, push or merge. Plan 2b consumes this branch.

---

## Builder's guesses (for the architect to confirm; Task 14 logs them as builder's calls)

| # | Guess | Why |
|---|---|---|
| G1 | The brief accepts question kind `own` in addition to the spec's five. | JobLens release 1 (BR1) gives the owner's own questions kind `own`, and 2b sends them; refusing them would break the integration. |
| G2 | The brief refuses any string with an email, a URL, or a run with ≥ 10 digits (phone), returning 422 with the field. Year ranges pass. | §6.1 and B2: a mechanical second guard, so a JobLens bug can't push contact details to Claude. Ten digits keeps "2019-2023" legal. |
| G3 | Limits are bounded as follows: `max_minutes` 1–45, `max_main_questions` 1–15, `max_follow_ups` 0–3, `max_llm_cost_usd` up to $2.00, 1–20 questions. Only the first `max_main_questions` questions go into the prompt. | §10 asks for "oversize limits" to be rejected. Planning only what can be asked keeps "all main questions done" well defined. |
| G4 | The result adds fields to §6.2: `error` (code), `llm_cost_usd` (the service's own estimate from brief prices), `audio.state` and `latency.n`/`measured: "server"`. Usage rows carry `kind` and optional `seconds`/`characters` for paid speech. | 2b needs to know when the audio is ready (AS5) and why a session failed. The service already computes the cost for the cap, and §5.5 asks for paid speech units. JobLens still prices from its catalog. |
| G5 | The cost cap works as follows. The director refuses the next LLM run when `spent + 1.25 × last turn > cap` and speaks a fixed closing by TTS. At 80% of the cap it adds a "close now" note. `max_tokens` is 200. | This makes the cap a hard ceiling (the A1/AD2 ceiling depends on it) without guessing the next turn exactly. |
| G6 | Time works the same way: at `max_minutes` a "time is up" note goes to the next turn, and at `max_minutes + 90 s` a fixed closing is spoken without the LLM. | "Finishes the current exchange and wraps up" (§5.4), with a hard bound if the model doesn't. |
| G7 | Follow-up and question limits are enforced by notes added to the next turn. Speech comes before the tool call, so an over-limit follow-up can't be blocked, only steered. Overruns are counted in logs. | AS5 forbids a blocking tool round trip before speech. |
| G8 | A tool result triggers an LLM run only when the response spoke no text. | Otherwise the candidate hears silence. It is rare and costs one round trip only in that case. |
| G9 | Notes are appended as an extra text block on the latest user message, prefixed "[Interviewer note, not said by the candidate]". A unit test on the Anthropic adapter's converted messages proves the block survives. If the adapter merges or drops it, the note is appended to the end of that message's text instead, which is still append-only (architect APR3). | Append-only keeps prompt caching. Mid-conversation system messages may not pass through Pipecat's Anthropic adapter. |
| G10 | The session folder lives on a named volume (`/data/sessions`). Restart recovery marks live sessions `failed` with `service_restarted`. `meta.json` stores prices and limits, never brief text. | AD4 must survive a container restart or recreate, and there should be as little content on disk as possible. |
| G11 | An unused session expires after 120 s as `failed` with `error: "not_connected"` (end_reason `error`). `DELETE` is refused while waiting or live. A waiting session blocks a second `POST /sessions` (409). | §5.7: the token expires in 2 minutes, and there is one session at a time. |
| G12 | The connect token comes in the `X-Session-Token` header or `?token=`, and works for one offer, then only for ICE PATCH on that `pc_id`. There is no reconnect: a dropped call ends `disconnected`. The rest of the API has no auth: it is bound to 127.0.0.1 and the compose network, and the browser only reaches it through JobLens's proxy. | §5.3/§5.7. A reconnect would need state the spec doesn't define. |
| G13 | Logs: IAssistPro's own records at INFO through `log_event` (free text redacted). Third-party libraries only at WARNING or above, loguru `diagnose=False`, uvicorn access log off (it would print `?token=`). | AS5 "INFO with a filter"; Pipecat's own INFO/DEBUG lines can carry text. The canary tests prove it. |
| G14 | The paid provider mapped is **Cartesia TTS** (`CARTESIA_API_KEY`, `TTS_VOICE` required). | It needs no extra install (the websocket extra is already there), so acceptance #6 is shown without adding a dependency. Deepgram STT would need `pipecat-ai[deepgram]`. |
| G15 | `LLM_PROVIDER=fake` (scripted) is a real factory option, used by the pipeline test and the free spoken e2e. | It lets the spoken e2e run through the real transport, STT and TTS at no cost (§10 "fake or real LLM"). |
| G16 | Audio is streamed to `audio.raw` on disk during the session (1 MiB chunks), then encoded to mono Ogg/Opus off the loop and deleted. A crash leaves `audio.state "none"`. | A 25-minute stereo buffer in RAM is ~96 MB. Keeping it on disk is safer within 7.6 GB, and partial audio after a crash isn't needed. |
| G17 | Standalone ports are HTTP 8770 and UDP 40020–40022 (the container listens on 8000). The standalone compose defaults to `DEV_CLIENT=1`, and JobLens's compose (2b) leaves it off. | It doesn't collide with JobLens prod and dev (AS4) or with 8000/8100. A dev client is needed to run it alone (§5.8 README). |
| G18 | Tests run inside the dev container (source mounted), and the e2e runs on the host with its own `e2e/package.json` (Playwright 1.56.1, the spike's version). | faster-whisper, piper and aiortc wheels are proven on Linux (spike). The fake mic needs a host browser. |
| G19 | Health stays 200 with `status` `loading_models`/`ready`/`error`, and a missing key for a selected provider is `error: "missing_key:<VAR>"`. | 2b can show "getting ready (n MB)" or "not configured" instead of a generic failure. |
| G20 | Example and fixture prices are Haiku 5.5's (input $0.10, cached $0.01, cache write $0.125, output $0.50 per MTok, from the current price list). Live sessions are capped at $0.05 each and $0.10 per test, under $0.30 in total. | Haiku is the default (AS2). JobLens sends its own catalog prices in real briefs. |
