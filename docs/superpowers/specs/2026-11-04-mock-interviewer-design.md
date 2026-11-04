# Mock interviewer and interview prep: design

Date: 2026-11-04. Owner: Rupak. Repos: **IAssistPro** (new: the live voice interviewer, branch `feat/mock-interviewer`)
and **JobLens** (interview prep, sessions and ratings, branch `feat/interview-prep`).

Decisions are logged in `docs/rupak_decisions.md` (this repo) with who made each: Rupak, the architect agent acting
for Rupak, or the builder.

## 1. Why

Rupak has an application in prod at **Interviewing**. JobLens already holds everything that makes interview prep
specific to the role: the JD, the tailored CV for that job, the master CV and the My CV answers. Today none of it
helps once the application is submitted. Rupak wants two things:

1. **A list of likely interview questions** built from the profile and the JD.
2. **A voice mock interview**: an agent asks those questions out loud, **follows up on what Rupak says like a real
   interviewer**, Rupak answers by voice, and afterwards each answer is **scored on relevance, specifics and
   structure, with a better answer suggested**.

Success: Rupak walks into the real interview having rehearsed the likely questions out loud, knows which answers were
weak and why, and has a stronger version of each, built from their own experience.

## 2. Goals, non-goals, anti-goals

Goals
- G1. Questions per application, generated from JD + tailored CV + master CV + My CV answers. **Generic** for now (no
  round types, no interviewer names, no recruiter notes) — Rupak: "keep it generic for now".
- G2. A **voice-only** mock interview (Rupak: "voice only is fine, camera not needed for now"), **adaptive**: the agent
  decides after each answer whether to follow up or move on (Rupak: "follow up on what I say like a real interviewer").
- G3. Built on **Pipecat** (Rupak: "see if pipecat is good for this"; assessed as a good fit).
- G4. **Local speech first** — local speech-to-text and text-to-speech on this laptop — **swappable to paid
  services by configuration** when results justify it (Rupak: "start with local but make it adaptable to paid
  services").
- G5. Each session's **recording is stored in Azurite** (Rupak), with its transcript.
- G6. Each answer scored on **relevance, specifics, structure**, with a **suggested better answer** (Rupak).
- G7. The interviewer lives in **its own repo (IAssistPro)** and JobLens uses it as a **service integration** (Rupak).

Non-goals (now)
- Video, camera, body language, eye contact.
- Round types (recruiter / technical / panel), interviewer personas, company research.
- Delivery scoring (pace, filler words, tone). Content only; see §9 "Later".
- Telephone, mobile, or any access from outside this laptop.
- Paid speech services in the first version (designed for, not wired as default).

Anti-goals (would be a failure even if it works)
- A suggested "better answer" that **invents** experience, numbers, employers, tools or dates Rupak does not have.
  Same rule as CV tailoring: truthful by construction, checked mechanically, not trusted to the prompt.
- **Spending money on its own.** Nothing paid runs without a click that shows its estimated cost (JobLens rule R23/C3).
- Personal data in the IAssistPro repo (it may be public: MIT licence). No CV, JD, transcript, recording, key or
  email is ever committed there; tests use invented profiles.
- An interviewer that breaks character, coaches mid-interview, reveals the "strong answer" notes, or talks over Rupak.
- A slow, fiddly start. Start to first question: one click on the job page, plus the browser's microphone permission
  the first time (R3: smooth journey, few clicks, guidance in the UI).

## 3. Constraints

- **Hardware:** Intel i5-1235U (10 cores / 12 threads), 16 GB RAM, Intel Iris Xe — **no CUDA GPU**. All local models
  run on CPU. Rupak: "use all resources of this laptop for this job": Docker sees all 12 threads, but each model's
  thread count is a setting tuned for speed (STT 4, TTS 4, smart-turn 1). On this hybrid CPU, 12 threads made Whisper
  2× slower (spike; AS5).
- **Same Windows machine, Docker Compose**, dev and prod stacks as today (JobLens feature 3). Ports bound to
  127.0.0.1 only (P4). Prod runs tagged releases only (D6).
- JobLens stack: FastAPI, SQLAlchemy 2, Alembic (head 0014), Postgres, Azurite, React/Vite/TanStack Query/Tailwind.
  LLM calls go through docutrack adapters and JobLens's spend meter (daily budget, hard cap, per-call `LlmCall` rows).
- Anthropic key already in `.env` / `.env.prod` (`ANTHROPIC_API_KEY`). Never print key values.
- Standing rules: never send Rupak's email address anywhere; never merge, push or tag without asking Rupak each time;
  commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; Leads data never enters this feature.
- Direct writes to the prod database are not allowed; prod changes arrive only through a tagged release and
  migration.

## 4. The shape

```
 Browser (JobLens UI, Chrome/Edge)                     IAssistPro "interviewer" container
 ┌──────────────────────────────┐   mic audio /       ┌──────────────────────────────────────────────┐
 │ Job page → Interview prep    │   agent audio       │ Pipecat pipeline                              │
 │  - questions                 │ ◄────────────────► │  transport in → VAD + turn detection          │
 │  - Start mock interview      │  (transport: §5.3)  │  → STT (local Whisper | paid, by config)      │
 │  - live captions, timer, End │                     │  → interviewer LLM (Claude, streaming, tools) │
 │  - results: scores, better   │                     │  → TTS (local Kokoro | paid, by config)       │
 │    answers, audio playback   │                     │  → transport out; recorder; transcript        │
 └──────────────┬───────────────┘                     └───────────────▲──────────────────────────────┘
                │ JobLens REST                                         │ HTTP on the compose network
 ┌──────────────▼───────────────────────────────────────────────────┐ │ (brief in; result, audio, usage out)
 │ JobLens backend: question sets, sessions, rating, spend, blobs   ├─┘
 │ Postgres (sessions, questions, ratings) · Azurite (recordings)    │
 └──────────────────────────────────────────────────────────────────┘
```

Responsibilities (decided with Rupak: separate repo, service integration):

| | IAssistPro interviewer | JobLens |
|---|---|---|
| Owns | the live call: audio transport, turn taking, STT, the interviewer's reasoning and follow-ups, TTS, recording, per-turn transcript, usage counting | questions, sessions, the brief, rating, spend metering, storage of recording and transcript, all UI |
| Receives | a **session brief** (§6.1) | the session **result** (§6.2) |
| Never receives | Rupak's email, Gmail data, Leads data, JobLens database access, other applications | — |
| Persists | nothing durable; a finished result is kept in a temp folder until JobLens collects it, then deleted (TTL 24 h) | everything durable |

The interviewer knows nothing about JobLens. Any app able to send a brief can use it.

## 5. IAssistPro: the interviewer service

### 5.1 Stack

- Python 3.12, **Pipecat** pinned to the latest stable release on PyPI at build time (1.10.x seen in Sept 2026; the
  builder pins exactly and records the version). FastAPI/uvicorn for the HTTP API. Packaged with `uv`, like JobLens.
- One Docker image `iassistpro-interviewer`. Model weights (Whisper, Kokoro, VAD/turn models) live in a named volume
  (`interviewer_models`), downloaded on first start, so code rebuilds don't re-download gigabytes. Startup reports
  "downloading models (n MB)" in its status endpoint so JobLens can show it.

### 5.2 Pipeline

- **Voice activity + end of turn:** Silero VAD (`stop_secs` 0.2) plus Pipecat's local smart-turn v3 (ONNX, no torch;
  spike: 12/12 turns, no cut-offs across 64 pauses); a silence threshold (~0.8 s) stays as a config fallback. Rupak must be able to pause to
  think without being cut off: the end-of-turn rule errs towards waiting. A turn ends only after a 1.5 s silence
  grace (overlapping Whisper), and an answer resumed within 2 s of a turn end is merged back into the same turn (AT1, AT2).
- **Interruptions:** Rupak speaking while the agent talks stops the agent's audio (barge-in). Headphones recommended
  in the UI to avoid the agent hearing itself (§5.3).
- **STT:** `faster-whisper` `base.en`, CPU, int8, beam 1, `cpu_threads` 4 (spike; Pipecat's defaults of beam 5
  and unset threads are overridden). `small.en` is the config fallback if your real voice transcribes badly (AS5).
- **LLM (interviewer):** Claude through Pipecat's Anthropic service, streaming. Default model `claude-haiku-5-5`
  (AS2: ~2 s faster per reply than Sonnet); `claude-sonnet-5-5` selectable in JobLens Settings ("Interviewer
  model", with "slower replies, ~6 s") and passed in the brief. Pipecat's lowest thinking setting for each model is
  kept (`disabled` for Haiku, `between_tools` for Sonnet); never adaptive thinking. Prompt caching on the brief when it
  reaches the minimum cacheable size; it isn't padded to get there.
- **TTS:** **Piper** `en_US-lessac-medium` (local, CPU; 0.19 s to first audio) is the default; Kokoro
  (`af_heart`, int8) is a config option (AS1). Sentence-level streaming. `stop_frame_timeout_s` is raised to 10 s,
  or Pipecat drops a reply whose audio takes more than 3 s to start.
- **Recorder:** `AudioBufferProcessor` (both sides, 16 kHz); the result is mono Ogg/Opus via `soundfile`, encoded
  once at session end off the event loop (stereo WAV only as a debug option).
- **Transcript:** every turn with speaker (`interviewer`/`candidate`), text, start/end offsets in ms from session
  start, and the `question_id` it belongs to (§5.4).

### 5.3 Transport (decided: SmallWebRTC, AS4)

**SmallWebRTC is the transport**, with a ~30-line aioice patch (bind a fixed UDP port range, advertise `127.0.0.1`).
aiortc/aioice are pinned exactly, and a canary test fails if the patched internals change. WebSocket stays as a
config fallback (`TRANSPORT=websocket`, no patch). Each stack publishes its own 3-port UDP range on 127.0.0.1
(prod 40000–40002, dev 40010–40012). The spike measured both options:

- **SmallWebRTC** (browser ↔ container, peer-to-peer, no Daily account). Best audio handling (browser echo
  cancellation, jitter buffer). Risk: WebRTC media from a container under Docker Desktop on Windows needs ICE to
  reach `127.0.0.1` through published UDP ports; may need host-candidate rewriting or a fixed port range.
- **WebSocket** (Pipecat's FastAPI WebSocket transport + its JS client serializer). Simple through Docker on
  localhost; latency is negligible on the same machine. Risk: weaker echo cancellation (headphones then required).

Pick rule: SmallWebRTC if it connects reliably from Chrome and Edge through Docker Desktop with a small, fixed config;
otherwise WebSocket. Either way the signalling/connect call goes **through JobLens's backend** (JobLens proxies the
offer or hands out a one-time session token), so the browser only ever talks to JobLens's origin for setup and the
interviewer port is bound to 127.0.0.1.

### 5.4 Interviewer behaviour

System prompt built from the brief. The interviewer:
- Opens with one short greeting naming the role and company, says roughly how long it will take, then asks the first
  question.
- Asks main questions in the brief's order (it may reorder to follow the conversation naturally, never skips the
  first one).
- After each answer decides: **follow up** (at most `max_follow_ups` per main question, default 2) when the answer is
  vague, claims something worth probing ("how did you measure that?", "what was your part versus the team's?"), or
  skips half of the question; otherwise **move on**.
- Signals structure through **tool calls** so the service knows where it is: `ask_question(question_id)` before a
  main question, `follow_up(question_id)` before a follow-up, `end_interview(reason)` to wrap up. The transcript's
  `question_id` comes from these calls, not from parsing text. The tool calls must not add an LLM round trip before
  speech: the reply's text and its tool call come in the same response, and the tool result doesn't trigger another
  LLM run (AS5).
- Opens each turn with a short clause ("Thanks. And how did you…"), so the first sentence reaches TTS fast (AS3).
- Never gives feedback, scores, hints or the "strong answer" notes during the session; stays in character; short
  turns (one question at a time, no multi-part monologues); polite, neutral, realistic.
- Ends when: all main questions are done, the time limit is reached (it finishes the current exchange and wraps up),
  the token budget is near (§5.5), Rupak presses End, or the connection drops. Each gives an `end_reason`.
- If Rupak says "can you repeat that" / "skip this one", it repeats or moves on.

### 5.5 Limits and usage

- Brief carries `max_minutes` (default 25), `max_main_questions` (default 8), `max_follow_ups` (default 2) and
  `max_llm_cost_usd` (default 0.50). The brief also carries the interviewer model's per-million-token prices
  (input, cached input, cache write, output) from JobLens's catalog, so the service enforces a **cost-weighted** cap
  (cached reads are cheap and a 25-minute session resends the transcript on every turn: a raw token cap would end
  sessions early). The service enforces all of them.
- Usage is written to the session's temp folder after every LLM turn, so a crashed or dropped session still reports
  what it spent (AD4).
- The service counts LLM usage per model (input, cached input, cache write, output tokens) and reports it in the
  result; JobLens turns it into spend. STT/TTS local cost is zero; paid providers report their usage units too
  (seconds, characters) so JobLens can price them later.

### 5.6 Provider configuration (local now, paid later)

Environment / settings in the service, one value each:
- `STT_PROVIDER=whisper` (later: `deepgram`, `assemblyai`, …), `STT_MODEL`
- `TTS_PROVIDER=piper` (or `kokoro`; later: `cartesia`, `elevenlabs`, …), `TTS_VOICE`
- `LLM_PROVIDER=anthropic`, `LLM_MODEL`
- `TRANSPORT=smallwebrtc|websocket`, `ICE_PORTS`, `ICE_HOST`; thread counts `STT_THREADS=4`, `TTS_THREADS=4`
- Provider keys (`DEEPGRAM_API_KEY`, …) read only when that provider is selected.

A small factory maps each value to a Pipecat service. Adding a paid provider = one mapping entry + its key, no other
code change. A brief may override `llm_model` (JobLens passes its setting); speech providers are a service setting.

### 5.7 HTTP API (service side)

- `GET /health` → status (`ready` | `loading_models` with progress), configured providers, version.
- `POST /sessions` (brief) → `{session_id, connect: {...}}` (transport-specific connect info, one-time token,
  expires in 2 minutes if unused).
- Transport endpoint(s) for the browser (WebRTC offer/answer or WebSocket), authenticated by the one-time token.
- `GET /sessions/{id}` → `{state: waiting|live|ended|failed, elapsed_s, current_question_id, end_reason?}`.
- `POST /sessions/{id}/end` → asks the interviewer to wrap up now.
- `GET /sessions/{id}/result` → transcript + usage + `end_reason` + timings (after `ended`).
- `GET /sessions/{id}/audio` → the recording bytes.
- `DELETE /sessions/{id}` → JobLens calls after storing; deletes temp files. Unclaimed results expire after 24 h.
- One live session at a time (a second `POST /sessions` while one is live → 409). This laptop can't run two.
- Logs: timings, sizes, counts and errors only — **never transcript text, brief content or audio**.

### 5.8 Repo layout (builder may adjust)

`src/iassistpro/` (api, brief/result schemas, pipeline factory, providers, interviewer prompt, recorder),
`tests/`, `docker/Dockerfile`, `README.md` (what it is, run it standalone with an example brief, provider config),
`examples/brief.example.json` (invented candidate), `docs/`.

## 6. The contract

Versioned JSON (`"version": 1`). JSON Schemas live in IAssistPro (`contracts/`); JobLens keeps a pydantic copy and a
contract test against the example files.

### 6.1 Session brief (JobLens → interviewer)

```json
{
  "version": 1,
  "role": {"title": "...", "company": "...", "summary": "3–6 sentences from the JD: what the role does and needs"},
  "candidate": {"name": "first name only", "summary": "5–10 sentences: experience, strengths, notable work"},
  "questions": [
    {"id": "q1", "text": "...", "kind": "must_have|cv_deep_dive|gap|behavioural|motivation",
     "probe_for": ["what a strong answer covers, short phrases, for follow-up decisions only"]}
  ],
  "limits": {"max_minutes": 25, "max_main_questions": 8, "max_follow_ups": 2, "max_llm_cost_usd": 0.50},
  "llm_prices_per_mtok": {"input": 2, "cached_input": 0.10, "cache_write": 2.50, "output": 10},
  "llm_model": "claude-haiku-5-5",
  "language": "en"
}
```

No email, phone, address, URLs, employer contacts or Leads data. JobLens builds `candidate.summary` from the master
CV (the existing AI Discover summary if present, else the master's opening paragraph); no extra paid call.

### 6.2 Session result (interviewer → JobLens)

```json
{
  "version": 1, "session_id": "...", "end_reason": "completed|time_limit|budget|ended_by_candidate|disconnected|error",
  "started_at": "...", "ended_at": "...",
  "turns": [{"speaker": "interviewer|candidate", "text": "...", "start_ms": 0, "end_ms": 0,
             "question_id": "q1", "kind": "main|follow_up|greeting|closing"}],
  "usage": [{"provider": "anthropic", "model": "...", "input_tokens": 0, "cached_input_tokens": 0,
             "cache_write_tokens": 0, "output_tokens": 0}],
  "audio": {"format": "ogg_opus|wav", "duration_ms": 0, "bytes": 0},
  "latency": {"median_reply_ms": 0, "p90_reply_ms": 0}
}
```

`latency` = time from end of Rupak's turn to the first agent audio; reported so we can judge local vs paid.

## 7. JobLens: interview prep

### 7.1 Where it appears

On the job page, an **Interview prep** section appears when the application status is `interviewing` (and stays
visible, collapsed, for `offer`/`rejected`/`withdrawn` sessions that already exist). Elsewhere, nothing changes.

Empty state guidance: "Prepare likely questions from this JD and your CV, then rehearse them out loud with a mock
interviewer." Button: **Prepare questions (~$0.0x)** with its estimate (R23/O6).

### 7.2 Question set

- One paid structured call (the generator's model setting, as C4) with: JD, the finalized tailored CV text for this
  job — the **submitted** version (`Job.submitted_pass_id`, what the employer holds), else the latest finalized pass,
  else the master — plus the **active** master CV text and My CV answers (the newest truth about Rupak).
- Output: **10–14 questions**, kinds `must_have`, `cv_deep_dive`, `gap`, `behavioural`, `motivation` (always includes
  "tell me about yourself" and "why this role/company"). Each question has:
  - `text`
  - `why_they_ask` (one line, tied to a JD requirement or CV line)
  - `strong_answer_covers` (3–5 short points) — built only from Rupak's materials; each point that relies on a fact
    carries its **source** (quoted words from CV/answers), checked mechanically like O1; a point needing a fact not in
    the materials becomes "[CHECK] you'd need an example of …" rather than an invented one.
- Shown as a list grouped by kind. Rupak can **untick** questions they don't want in the mock (the mock uses ticked
  questions, in list order, up to `max_main_questions`) and can **add their own** question (text only). **Regenerate**
  replaces the set (with cost shown, after a confirm); it keeps Rupak's own questions and never deletes past sessions.
  Unticks, own questions and order are saved server-side as they change (AD1).
- Fact checks on `strong_answer_covers` (and on ratings, §7.4) reuse JobLens's existing number/employer/tool/date
  checker (My CV B5/O3), not a new one (AD5).
- Stored per job: `interview_question_sets` (job_id, created_at, model, master version, questions JSON, cost) — the
  newest set is current.

### 7.3 Mock interview

- **Start mock interview** (shown when a question set exists and the interviewer is ready). Shows the typical cost
  and the ceiling of the whole session including rating: "Start mock interview (~$0.35, at most $0.60)"; ceiling =
  `max_llm_cost_usd` + rating estimate (AD2). Refused with the usual message if `ensure_can_start` blocks, **and**
  refused unless today's spend + the ceiling fits under the hard cap, with the numbers in the message (A1). One live
  session at a time.
- First time only: a short check panel — "Use headphones", microphone picker, a level meter, and "Plugged in and on
  Best performance power mode gives quicker replies" — then Start.
- JobLens creates `mock_sessions` row (`starting`), builds the brief (§6.1) from the ticked questions, calls
  `POST /sessions`, returns connect info to the browser, which connects (§5.3).
- Live screen (full page, minimal): role and company, timer against the limit, current question number ("Question
  3 of 8 · follow-up"), live captions of both sides, mic level, **End interview**. No scores shown live.
- If the interviewer is not running/ready: the section says so in one line with the command to start it (dev) and
  question prep still works. If it is loading models: "Interviewer is getting ready (downloading voice models, n MB)".

### 7.4 After the session

1. JobLens fetches the result and, **in this order** (A1): stores the transcript in the row; then records the
   interviewer's LLM usage as `LlmCall` rows (phase `interview`, job_id set) priced from the catalog. The audio is
   encoded after the session (it can take a minute), so JobLens fetches it when the service reports it ready, stores
   it in Azurite at `interviews/{job_id}/{session_id}.{ext}`, and only then `DELETE`s the service copy. Rating doesn't
   wait for the audio; "play from here" appears once it's stored (AS5).
   Session → `rating`. If recording the usage trips the hard cap (`record()` commits, then raises
   `HardCapExceeded`), the error is caught and the session goes to `rating_failed` with "Today's hard cap was
   reached. Your recording and transcript are saved; Retry rating after midnight." The same message applies if the
   rating call itself is refused by the cap.
   If no candidate answered any main question, no rating call is made: the session ends as "Nothing to rate", with the
   recording kept (AD3).
2. **Rating** (automatic: it is part of the session the button already priced). One structured call (generator model)
   with the questions (and their `strong_answer_covers`), the transcript grouped per main question (follow-ups
   included), and Rupak's materials. Per main question:
   - `relevance`, `specifics`, `structure`: each **1–5** with a one-line reason.
   - `better_answer`: a stronger version **in Rupak's voice, using only facts from their materials or what they said in
     the session**; same source check and "[CHECK]" rule as §7.2; never invents numbers, employers, tools or dates.
   - `missed`: points from `strong_answer_covers` they didn't make.
   - Questions not reached are listed as "not asked".
   - Session summary: 2–3 strengths, 2–3 things to work on, average of each score.
   Session → `rated` (or `rating_failed` with **Retry rating**, transcript and audio kept).
3. Results view: per question card — question; Rupak's answer (transcript, with **play from here** on the recording);
   the follow-ups and their replies; the three scores with reasons; missed points; the better answer (copy button;
   [CHECK] marks highlighted). Top: summary and averages. A list of past sessions for the job with date, length and
   averages, so improvement is visible across attempts. **Delete session** removes its row and recording.

### 7.5 Storage and spend

- Migration 0015 (release 1, A3): `interview_question_sets`; widens the `LLM_PHASES` CHECK constraint (`models.py`)
  with `interview_prep`.
- Migration 0016 (release 2): `mock_sessions` (job_id, question_set_id, status
  `starting|live|ended|rating|rated|rating_failed|failed`, started_at, ended_at, end_reason, brief JSON, turns JSON,
  ratings JSON, summary JSON, audio_blob_path, audio_format, duration_ms, latency JSON, cost_usd); adds `interview`,
  `interview_rating` to `LLM_PHASES`.
- Spend: question generation and rating through the metered adapters (phases `interview_prep`, `interview_rating`);
  interviewer usage recorded after the session (phase `interview`). Today's spend card includes them. The three
  interview phases are **exempt from the per-job cap** (A1): `job_spent` excludes them and their recorders skip the
  job-cap check; the daily budget and hard cap apply as usual. The prep section shows "Interview prep spend on this
  job: $x".
- Recovery: a session left `starting`/`live` when JobLens restarts is reconciled with the service (`GET
  /sessions/{id}`); if the service has a result, it's collected; otherwise `failed` with "interrupted". If no usage
  can be recovered, JobLens records an estimated `LlmCall` at the session cap (error "usage lost") so the spend is
  never missing (AD4).

### 7.6 Compose and releases

- **One interviewer per stack** (A2). JobLens's `docker-compose.yml` gains an `interviewer` service built from
  `${IASSISTPRO_DIR:-../IAssistPro}` with **no `image:` key** (images are project-scoped, like JobLens's), port
  127.0.0.1-bound (HTTP plus the stack's UDP range, §5.3), its own per-project models volume (base.en + one Piper
  voice), env via explicit `environment:` entries, not `env_file:` (only
  the keys it needs). JobLens reaches it on the compose network (`INTERVIEWER_URL`).
  - **Prod:** profile `app`, `restart: unless-stopped`. `ops/prod.ps1 up <joblens-tag> -Interviewer <iassistpro-tag>`
    checks out a detached worktree `..\IAssistPro-prod` at that tag (as for JobLens) and passes its **absolute** path
    as `IASSISTPRO_DIR`. Tag required (no default; a branch only with `-AllowBranch`); the last tag is recorded under
    `backups/`, git-ignored; `status` prints it.
  - **Dev:** profile `interviewer` (not `app`), started only when testing (`docker compose --profile interviewer up
    -d interviewer`), from whatever branch `..\IAssistPro` has checked out, and stopped afterwards, so normally only
    prod's copy is resident in 7.6 GB.
- `prod.ps1` has no idle check today: one is written (passes, judge, Discover run, live mock session) and refuses a
  deploy while any of them runs.
- **Two prod releases** (A3). Release 1 (JobLens only): question prep (§7.2), migration 0015, the per-job-cap
  exemption, the idle check; no interviewer service in prod and no Start button or "interviewer not running" line.
  Release 2: the interviewer after the spike and a live dev session pass, migration 0016, the first IAssistPro tag.
  Release 1 is built and offered to Rupak first.
- Releases of either repo happen only when Rupak asks (standing rule).

## 8. Privacy and safety

- Local STT keeps Rupak's voice on the laptop; with a paid STT provider later, Settings shows "Your voice is sent to
  <provider> during mock interviews" next to the provider.
- Claude receives the brief and transcript, as it already receives the CV and JD for tailoring.
- IAssistPro: no personal data in the repo, logs without content, temp results deleted after collection or 24 h.
- Recording consent is implicit (Rupak records themselves); the live screen shows a red "Recording" dot.

## 9. Later (not now)

Delivery scores (pace, filler words, long pauses — from the transcript timings); round types and personas; recruiter
notes in the brief; paid STT/TTS defaults; practising a single question repeatedly; camera.

## 10. Testing (what must be proven)

IAssistPro:
- Unit: brief validation (rejects missing questions, oversize limits, unknown version); provider factory mapping;
  limit enforcement (time, tokens, follow-ups per question); tool-call → `question_id` tracking; end reasons; temp
  result expiry; one-session-at-a-time 409; logs contain no transcript text (test scans log output for a canary).
- Pipeline test with Pipecat's test utilities and a fake LLM: a scripted conversation produces the right turns,
  question ids and end reason.
- **Spoken end-to-end test** (marked slow): synthetic candidate answers (pre-generated WAV files of an invented
  candidate, committed) fed through the real transport to the real local STT/TTS with a fake or real LLM (real only
  when a key is present and the test is explicitly enabled); asserts a full session completes and reports latency.
- Spike report (`docs/spike-transport-latency.md`): transport choice, Whisper model choice, measured median/p90
  reply latency on this laptop, CPU use.

JobLens:
- Question generation and rating with fake LLMs: kinds present, sources checked, [CHECK] on unsupported facts,
  invented-fact detection flags a number/employer/tool/date not in the materials.
- Session lifecycle against a fake interviewer service: start, budget refusal, interviewer down/loading, collect
  result, audio stored in Azurite, usage recorded as spend, rating failure + retry, restart recovery, delete.
- Spend rules (A1, AD3, AD4): interview phases don't count towards or get blocked by the per-job cap; Start refused
  when the ceiling doesn't fit under the hard cap; a hard-cap trip while recording usage leaves audio, transcript
  and usage stored and the session `rating_failed` with the cap message; no rating call when there's no answer; lost
  usage recorded as an estimate at the session cap.
- Contract test against IAssistPro's example brief/result.
- Web: Vitest for the prep list (untick, add, regenerate) and results view; Playwright check with faked API routes
  (no spend) for the live screen states and results.
- e2e rules unchanged: never against prod, never leaves data behind.

## 11. Acceptance

1. On an Interviewing application in dev, Rupak clicks Prepare questions, then Start mock interview, and is asked
   the first question within 10 seconds of Start (models already downloaded).
2. A 15–25 minute session runs without the agent cutting Rupak off mid-thought or talking over them (with headphones);
   it asks follow-ups that refer to what they actually said.
3. Reply gap (end of your speech → first interviewer audio, measured in the browser) with the defaults (Piper +
   Haiku, base.en, 1.5 s turn grace): median ≤ 4.5 s and p90 ≤ 5.5 s on this laptop, plugged in on Best performance
   (AT3; was 4.0 / 5.0 s under AS3). The original 3.5 s target is left to paid streaming speech later (R6).
4. After End, results appear within ~1 minute: scores, reasons, missed points, better answers with no invented facts,
   and the recording plays from each answer.
5. Spend for the session appears in today's spend.
6. Swapping `STT_PROVIDER`/`TTS_PROVIDER` to a paid provider needs config and a key only (shown by a unit test of
   the factory with one paid provider mapped, even if not used live).

## 12. Left to the builder

Exact Pipecat version and classes (1.15.0 at the spike); file layout; UI layout details within §7; prompt wording within §5.4/§7.2/§7.4 rules.
