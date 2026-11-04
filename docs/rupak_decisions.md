# Rupak's decisions: mock interviewer and interview prep

This log lists every product decision on this feature, and who made it:

- **Rupak**: you said it in the conversation.
- **Architect**: the architect agent acting for you, because you delegated decisions until completion.
- **Builder**: an engineering choice the builder made itself.

Please review the architect's and builder's entries in particular; tell the builder to change any you disagree with.

Design: `docs/superpowers/specs/2026-11-04-mock-interviewer-design.md`.

## Rupak (2026-11-04)

| # | Decision |
|---|---|
| R1 | Build an interview prep flow for applications at Interviewing: a list of questions from the profile and JD, and a mock interview where a voice agent asks them and rates the answers. |
| R2 | Voice only; no camera for now. |
| R3 | Store each recorded session in Azurite. |
| R4 | The agent is adaptive: it follows up on what you say, like a real interviewer. |
| R5 | Assess Pipecat for it (assessed: a good fit). |
| R6 | Local speech-to-text and text-to-speech first, adaptable to paid services once results are good. |
| R7 | Score each answer on relevance, specifics and structure, and suggest a better answer. |
| R8 | The interviewer lives in its own repo, `C:\2026\rkumar\IAssistPro` (`main` is the default branch; work happens on a feature branch); JobLens uses it as a service integration. |
| R9 | Keep the questions generic for now (no round types or recruiter notes). |
| R10 | An architect agent represents you until completion. Use all of this laptop's resources for the job. |

## Builder's calls in the design (2026-11-04)

| # | Decision | Why |
|---|---|---|
| B1 | Work happens on `feat/mock-interviewer` (IAssistPro) and `feat/interview-prep` (JobLens). Nothing is pushed, merged or tagged without asking you. | Standing rule. |
| B2 | IAssistPro is treated as public: no CV, JD, transcript, recording, key or email ever goes into it; tests use an invented candidate. | It has an MIT licence and `gh` can't confirm its visibility here. |
| B3 | The rating lives in JobLens, not the interviewer. | Budget, spend tracking and your full profile are in JobLens; the interviewer stays generic and reusable. |
| B4 | "All resources": Docker already sees all 12 CPU threads and ~7.6 GB RAM (WSL default); local models use every thread. The WSL memory limit is not raised. | Raising it needs `wsl --shutdown`, which stops prod; 7.6 GB fits Whisper + Kokoro + JobLens. If memory runs short, you can add `memory=12GB` to `%USERPROFILE%\.wslconfig` at a quiet moment. |
| B5 | The session cap is cost-weighted (`max_llm_cost_usd`, prices passed in the brief), not a raw token count. | A 25-minute session resends the cached transcript each turn; a 120k-token cap would end it around question 4. |
| B6 | Prep and rating use the CV version you submitted (`submitted_pass_id`), else the latest finalized one, plus your active master and My CV answers. | That's the CV the employer holds; the active master is the newest truth about you. |
| B7 | The interviewer container gets only the keys it needs (explicit `environment:`), not the whole `.env`. | It has no use for OpenAI or Gmail settings. |
| B8 | `ops/prod.ps1` gets a real idle check (passes, judge, Discover, live mock session) before deploying; the last interviewer tag is recorded under `backups/`. | The script has no idle check today; it was done by hand before each deploy. |

## Decided by the architect (2026-11-04, design review)

| # | Question | Decision | Why |
|---|---|---|---|
| A1 | Per-job cap, daily headroom, and a hard-cap trip while collecting | **Interview phases are exempt from the per-job cap**: `job_spent` leaves out `interview_prep`, `interview` and `interview_rating`, and their recorders don't check it (rows still carry `job_id`, so the section shows "Interview prep spend on this job: $x"). No new cap: each session already has its own hard ceiling (`max_llm_cost_usd` + rating). **Start refuses** unless today's spend + the session's ceiling (session cap + rating estimate) fits under the hard cap, as well as the usual `ensure_can_start`; the message gives the numbers ("$0.42 left before today's hard cap; a session can cost up to $0.60"). **Collection order**: store audio and transcript first, then record usage; a `HardCapExceeded` from `record()` is caught (the rows are already committed) and the session goes to `rating_failed` with "Today's hard cap was reached (a Discover run or another call spent at the same time). Your recording and transcript are saved; Retry rating after midnight." Never silent, never lost. | The job cap guards against a runaway tailoring loop. Your one Interviewing job has probably used much of its $1 on tailoring already, so counting interview spend would block the feature on exactly the job that needs it. The interviewer spends real money before JobLens records it, so the only real guard is checking headroom before Start. Prod's daily Discover can spend at the same moment, so a trip can still happen and needs its own path. |
| A2 | One shared interviewer container or one per stack? | **One per stack, each built from its own checkout; dev's starts only when asked.** Compose builds `interviewer` from `${IASSISTPRO_DIR:-../IAssistPro}`, with no `image:` key, so images are named per project as JobLens's are. **Prod**: `ops/prod.ps1 up <joblens-tag> -Interviewer <iassistpro-tag>` checks out a detached worktree `..\IAssistPro-prod` at that tag (as it does for JobLens), passes its absolute path as `IASSISTPRO_DIR`, records the tag under `backups/`, and runs the service in profile `app` with `restart: unless-stopped`. A tag is required: there is no default, and a branch only with `-AllowBranch`, as today. **Dev**: profile `interviewer` (not `app`), started with `docker compose --profile interviewer up -d interviewer` when testing, from whichever branch `..\IAssistPro` has checked out (e.g. `feat/mock-interviewer`), and stopped afterwards. Each stack has its own models volume (downloaded once each, under 1 GB). | One shared service would let a dev branch replace or crash prod's interviewer, and prod is used daily. Two always-on copies would sit in 7.6 GB for nothing. With dev on demand, only one copy is resident normally. A relative path in prod would build from the dev working tree; the worktree and absolute path fix that. |
| A3 | Release phasing | **Two prod releases.** Release 1 (JobLens only): question prep (§7.2), migration 0015 (question sets + `interview_prep` phase), the prep list, the per-job-cap exemption and the `prod.ps1` idle check; no interviewer in prod, and no Start button or "interviewer not running" line. Release 2: the voice interviewer after the spike and live dev test, migration 0016 (`mock_sessions`, `interview`/`interview_rating` phases), the first IAssistPro tag. The builder does release 1 first. | Your interview date is unknown and may be soon. Questions plus strong-answer points are useful on their own: you can rehearse out loud from the list today. The voice side carries the spike risk (WebRTC through Docker, CPU latency) and must not hold that up. Splitting the migration keeps `mock_sessions` from being frozen in prod before the spike shapes it. |

## Approval by the architect (2026-11-04), with changes

| # | Change | Why |
|---|---|---|
| AD1 | Unticks, your own added questions and their order are saved on the set immediately (server-side). **Regenerate** keeps your own questions and asks before replacing. | Typed work is never lost (as B3 in JobLens); a refresh must not undo your choices. |
| AD2 | The Start button shows the typical cost and the ceiling: "Start mock interview (~$0.35, at most $0.60)". The ceiling is the session cap plus the rating estimate, the same number A1 checks. | You watch spend while testing (O6). A live session's cost varies, so the ceiling is the honest number. |
| AD3 | No paid rating when the transcript has no candidate answer to a main question (e.g. dropped in the first minute): the session ends as "Nothing to rate", with the recording kept. | Nothing spends money for no result. |
| AD4 | The service writes usage to its temp folder after each LLM turn, so a crashed session still reports what it spent. If no usage can be recovered, JobLens records the session cap as an estimated `LlmCall` (`estimated` flag, cost = cap, error "usage lost") and shows it in today's spend. | Money spent must never go missing from the meter; overstating is the safe side (as I6 in JobLens). |
| AD5 | Every rating and question-prep point that cites a fact is checked mechanically against your materials and, for ratings, the transcript. A number, employer, tool or date found in neither becomes [CHECK] (same check as B5/O3 in JobLens, reused, not rewritten). | Truthfulness by check, not by prompt; one checker to maintain. |

## Plan approval by the architect (2026-11-04, Release 1)

Plan: `C:\2026\rkumar\JobLens\docs\superpowers\plans\2026-11-04-interview-prep.md`. Approved with changes.

| # | Change or ruling | Why |
|---|---|---|
| AP1 | The idle check opens prod's database in a read-only transaction with a 5 s statement timeout, with a test that a write fails (Task 6). | It runs dev's code against prod; prod is only ever read there, as Q2 did for `promote`. |
| AP2 | Migration 0015's downgrade refuses while `interview_prep` spend rows exist, instead of deleting them (Task 1). | Those rows are real money; a downgrade must never make spend go missing (AD4). |
| AP3 | G2 and G3 confirmed: a point with a bad source is kept and marked [CHECK] with its reason, not dropped. The JD is **not** a fact source. | Prep points are advice, not CV text, so marking keeps their value. A JD tool you don't have is exactly what must be flagged; over-flagging is the safe side. |
| AP4 | G9 and G11 confirmed: Regenerate sends the set it replaces (409 if stale) and asks first; a model failure gives a clear 502 and changes nothing. Two tabs pressing Prepare at once can still pay twice (the second result is refused as stale); accepted as rare. | No blind spend from a stale tab; a clear message. |
| AP5 | G13 confirmed, with no override switch. "Review" passes count as idle. | A stopped prod has nothing in flight, and a pass waiting for your review isn't work in progress. |
| AP6 | G14 confirmed: the live e2e (cap $0.30) runs only on a dev Interviewing application without a set, deletes what it made, and reports its cost. It never touches prod. | You see the real cost once; no data is left behind. |
| AP7 | Planner note (a) accepted: the job page's "Cost so far" includes prep spend, because it is the job's true total and nothing compares it to the cap. Note (b) accepted: My CV's raw 500 stays out of scope. | Truthful totals; YAGNI. |
| AP8 | G1, G4–G8, G10, G12 and G15 accepted as written. | They match the design (§7.1–7.2, AD1, A3). |

## Builder's calls during implementation (release 1, JobLens)

Branch `feat/interview-prep` in JobLens. The plan's guesses G1–G15 were built as written (confirmed by AP3–AP8); the
calls below are the ones made while building.

| # | Call | Why |
|---|---|---|
| BR1 | G1–G15 built as the plan states: two fixed questions by schema plus up to 12 more; bad-source points kept and marked [CHECK]; the JD is not a fact source; contact details stripped; prep only at Interviewing, edits at any status; one ordered list with headings; client-made own ids; set stores its CV label and cost; regenerate names the set it replaces; estimate = mean of the last 3 prep calls, else $0.08; 502 on model failure; idle check in Python, skipped when prod's app is down; live e2e skips without an Interviewing application; this log. | Plan and AP3–AP8. |
| BR2 | A test that migration 0015's downgrade refuses while `interview_prep` spend exists (not in the plan). | AP2 made it a requirement; nothing else exercised it. |
| BR3 | The v0 fallback needs a clean CV file too; a finished v0 without one falls through to the master. | There is nothing else to read; v0 started from the master anyway. |
| BR4 | Empty questions and blank points are dropped before the 12-question and 5-point cuts; a question with text but no points is kept; a blank fixed question gets a default wording. | Keeps as many real questions as the model gave (spec: 10–14). |
| BR5 | A point the model itself marked [CHECK] shows no reason line; the reason line comes only from the two mechanical checks. | The point's text already says what example is needed. |
| BR6 | In `prod.ps1 restore`, the idle check runs before the release checkout (the first thing that touches prod), and `status` prints the check without refusing. | Nothing is touched while prod is busy. |
| BR7 | An own question edited to blank stays on screen with "Type your question or remove it" and isn't saved until it has text; Regenerate is greyed out away from Interviewing. | The server refuses empty own questions; prep is Interviewing-only (G5). |
| BR8 | Dev had no Interviewing application, so for the one live run the builder (not the test) moved dev's one Submitted job to Interviewing by SQL and put it back (status and `updated_at`) afterwards. Prod untouched. | The live run had to happen once; the test itself never changes a status (G14). |
| BR9 | Live run on dev: 2 calls, $0.21 in total ($0.11 + $0.10), 14 questions per set; both sets deleted, spend kept. The first-ever estimate stays $0.08 (G10), so the very first button under-states a call by about 3 cents; after one call it shows the real mean. | Recorded so you know the real cost. |
| BR10 | The job page's "Cost so far" already summed every phase, so AP7 needed no change; Discover's auto-promote estimate also sums every phase and was left as is. | Prep on a few jobs nudges that estimate up slightly: the safe side. |

## Spike rulings by the architect (2026-11-04)

Spike: `docs/spike-transport-latency.md` (commit 39bc097).

| # | Ruling | Why |
|---|---|---|
| AS1 | **TTS default: Piper** (`en_US-lessac-medium`); Kokoro stays a config option. In the release-2 live dev session you hear Piper; if its voice spoils the realism for you, you can switch to Kokoro with one setting and accept ~5 s replies. | A realistic interviewer is mostly about timing: a 5–8 s silence after every answer feels far less real than a slightly plainer voice. Kokoro also caused talk-overs (barge-in) when replies ran long. |
| AS2 | **Interviewer LLM default: Haiku 5.5.** Sonnet 5.5 is a JobLens setting ("Interviewer model", labelled "slower replies, ~6 s"). Each model keeps Pipecat's lowest thinking setting. Rating stays on the generator model (Sonnet). | Follow-ups (R4) are judged against each question's `probe_for` notes, which gives Haiku what it needs to probe. Sonnet's ~2 s extra per reply and its longer first sentences hurt the interview itself. Depth matters most in the rating and better answers, which stay on Sonnet. You can compare the two in dev with one setting. |
| AS3 | **Acceptance #3 is now median ≤ 4.0 s and p90 ≤ 5.0 s** (browser-measured, Piper + Haiku + base.en). The interviewer is prompted to open each turn with a short clause. The check panel suggests plugging in and using the Best performance power mode. **No filler or backchannel sounds.** Paid streaming speech (the remaining route under 3.5 s) waits until the local results justify it (R6). | The spike reached 3.9 / 4.0 s and the cheap tricks can only help. A canned "mm-hm" fires on false turn ends and repeats robotically. Paid speech costs money on every session and needs pricing and a visible cost first. |
| AS4 | **Transport: SmallWebRTC** with the aioice patch. aiortc/aioice are pinned exactly, with a canary test on the patched internals. WebSocket stays a config fallback (`TRANSPORT=websocket`). UDP ranges are 127.0.0.1-bound, 3 ports per stack: prod 40000–40002, dev 40010–40012. | 14/14 connects, plus the browser's echo cancellation. The fallback means a dependency upgrade that breaks the patch costs one setting, not a release. Separate ranges let both stacks run (A2). |
| AS5 | **Design updates:** STT is `base.en`, int8, beam 1, 4 threads, and thread counts are settings (this replaces "all threads"; Docker still has all 12). Re-check `base.en` on your real voice in the live dev session; that recording stays local and is never committed (B2), and `small.en` is the fallback, re-measured against AS3. Turn detection is Silero (0.2 s) + smart-turn v3. Pipecat's `stop_frame_timeout_s` goes up to 10 s. Tool calls must not add an LLM round trip before speech. Logs run at INFO with a filter (the canary test stays). The recording is mono Ogg/Opus, encoded after the session; JobLens stores the transcript and usage first, and rating doesn't wait for the audio. | Each follows a measured result or a trap the spike found; the audio change keeps "results within ~1 minute" (acceptance #4) despite ~1 minute of encoding. |

## Plan approval by the architect (2026-11-04, Release 2a/2b)

Plans: 2a `docs/superpowers/plans/2026-11-04-interviewer-service.md` (IAssistPro), 2b `C:\2026\rkumar\JobLens\docs\superpowers\plans\2026-11-04-mock-interview.md`. Both approved with changes.

| # | Change or ruling | Why |
|---|---|---|
| APR1 | **One contract, 2a's "Service interface" is the source of truth**; 2b's C1–C10 were rewritten to match it exactly. Changes to 2b: container port 8000 (not 8765; `JOBLENS_INTERVIEWER_URL=http://interviewer:8000`, host ports 8010/8110 → 8000). Create returns 201 with `expires_at`, and 409 `busy` also covers a *waiting* session; 503 `not_ready` reads as loading; health `error` gets its own line. The token goes in `X-Session-Token`, not Bearer, from JobLens's proxy server-side, so the browser never sees it. Status uses `question_number`/`questions_total`/`turn_kind` (not `current_kind`). The result is served for `ended` and `failed`, with `error`, `llm_cost_usd`, `audio.state` and nullable fields, and only `kind == "llm"` usage rows are priced. Audio pending is 409 `audio_pending` (not 202). Compose builds `target: runtime` and mounts an `interviewer_sessions` volume at `/data/sessions` (so a restart keeps usage, AD4). Examples are at `contracts/examples/`. The e2e WAV is generated (`e2e/out/session_live.wav`), not committed. 2a's interface now also states the RTVI captions, candidate-turn `question_id`, uncached `input_tokens` and the header rule. | Two plans written in parallel must not meet for the first time in the live test. |
| APR2 | **Own questions go to the interviewer as kind `own`** (2a G1 kept; 2b H9 changed), with empty `probe_for`. 2b's `assert_brief_clean` also applies 2a's three contact patterns exactly. | Labelling your own question "behavioural" would be a small untruth to the interviewer for no gain. One rule on both sides means a brief JobLens sends is never refused at Start. |
| APR3 | 2a G9 confirmed, with a test on the adapter's converted messages and a fallback (note appended to the same message's text). The JS header question doesn't matter for JobLens (APR1); `?token=` stays for the dev client only, with the access log off (G13). | The planner flagged both as unverified Pipecat behaviour; each now has a check and a way out. |
| APR4 | 2a G5–G7 confirmed: the cost cap is hard (the A1/AD2 ceiling depends on it); time and follow-up limits are soft, steered by notes, with overruns counted. G11, G12 (no reconnect), G14 (Cartesia as the mapped paid TTS) and G16 confirmed. | A cap that can't stop speech mid-sentence is a fair trade for no extra round trip (AS5); only money needs a hard stop. |
| APR5 | 2a's latency acceptance: one rerun allowed, no tuning; a miss comes to me with the breakdown. Confirmed. | Tuning to pass a test hides the real number you'll live with. |
| APR6 | 2b branch ruling confirmed: `feat/mock-interview` from the tip of `feat/interview-prep`. Any Release 1 review fix lands on `feat/interview-prep` first and is merged into `feat/mock-interview`, never the other way round. | Release 1 must stay shippable on its own (A3). |
| APR7 | 2b H5 (`record(..., cost=)`), H7 (automatic rating skips the soft gate; only the hard cap stops it; Retry checks the budget), H10 (rating fact sources = your materials + **your** turns only) and H18 (idle check skips a schema without `mock_sessions`) confirmed. | H7 is what A1/AD2 promised: the Start click priced the rating. H10 keeps the interviewer's words from becoming "facts" about you. |
| APR8 | 2b H15 confirmed: JobLens supports SmallWebRTC only, so the `TRANSPORT=websocket` fallback is for standalone use. This narrows AS4: aioice is pinned, so the patch can only break on a deliberate upgrade, and the canary catches that first. | A WebSocket proxy in JobLens is work for a case that pinning prevents (YAGNI). |
| APR9 | 2b H20 confirmed with one condition: take a dev `pg_dump` to `backups/` before the one-off dev status move, and put the status back in `finally`. Cap $0.80. | Same insurance as Q4; the test leaves your dev data as it found it. |
| APR10 | Other guesses (2a G2–G4, G8, G10, G13, G15, G17–G20; 2b H1–H4, H6, H8, H11–H14, H16, H17, H19, H21) accepted as written. | They match the design and earlier rulings. |

## Final review (Release 1, 2026-11-04)

The final reviewer said "ready to merge"; the architect re-graded two findings to Important and both were fixed, each
with a test that failed first.

| # | Fix | Test |
|---|---|---|
| FR1 | The fact check took a My CV **question** as evidence ("Have you run Kubernetes in prod? Answer: no" let "Mention your Kubernetes experience" pass, and a quoted source could match the question). Now only the answers' own words count as evidence; the question still goes into the prompt for context. | `test_my_cv_question_text_is_not_evidence` |
| FR2 | An own question saved just after a regenerate (from another tab, or while the new set arrived) was refused as stale and vanished silently. Now that save is put onto the new set and the page says so; if even that fails, it names the questions to add again. | vitest "own questions after a stale save"; e2e "keeps a question typed after another tab regenerated" |

Deferred minors (not fixed; for you to decide):

| # | Minor |
|---|---|
| DM1 | The idle check doesn't see a prep call in flight: if prod were redeployed during the ~30 s call, that call's spend could go unrecorded (small window). |
| DM2 | In `prod.ps1 up`, the idle check runs before the backup; it could move to just after it. |
| DM3 | Two tabs pressing Prepare at once both pay; the second result is refused as stale (accepted in AP4). |
| DM4 | The phone pattern also strips 10+ digit figures (money, IDs) from the CV, so a point quoting one gets a false [CHECK]. |
| DM5 | The email is stripped only by pattern, not also by matching the owner's known address. |

## Builder's calls during implementation (release 2a, IAssistPro)

Guesses G1–G20 from the 2a plan stand as confirmed by APR1–APR10 (G1 kind `own`, G2 contact guard, G3 limit bounds,
G4 result fields, G5 hard cost cap, G6 time limit, G7 soft follow-up limits, G8 re-run only on silent tool calls,
G9 notes as an extra block (the adapter keeps it, so the primary path is used), G10 session volume, G11 120 s unused
expiry, G12 one-time token, G13 content-free logs, G14 Cartesia as the paid example, G15 fake LLM, G16 audio on disk,
G17 ports 8770 / 40020–40022, G18 tests in the dev container, G19 health states, G20 Haiku prices). No contract change
was made: the "Service interface" is as planned.

Calls made while building (the full list, with what each costs if wrong, is in the branch's ledger):

| # | Call | Why |
|---|---|---|
| B1 | Pipecat 1.15's current API: `PipelineWorker` + `WorkerRunner` and `EndWorkerFrame` (not the deprecated task/runner); RTVI left on for captions. | The plan's names are deprecated in 1.15. |
| B2 | The director reads spoken text and usage from the LLM's synchronous `on_before_push_frame`, and labels travel in the frame stream (`TurnLabelFrame`), not through observers. | Observers are queue-fed in 1.15 and lag the tool handler. |
| B3 | The budget hard stop is checked before each LLM run; the 1 s timer checks only the time hard stop. `/end` interrupts, waits 0.3 s, then speaks the fixed closing. | A timer budget stop would cut the candidate off right after a question; without the settle the closing merged with the cancelled reply. |
| B4 | 422 errors keep only type/loc/msg; route templates may contain `{}` in logs; the uvicorn access log is off. | Validation context and input can hold brief text. |
| B5 | An expired connect token is caught on use as well as by housekeeping. A session ending with `error` is state `failed`. | The lock frees even if housekeeping hasn't run. |
| B6 | The reused `iassist_models` volume was root-owned (spike), so it was chowned once to the runtime user (uid 10001). | The non-root image could not write model caches. |
| B7 | The dev client sends the token in `X-Session-Token` and as `?token=`. | The JS transport's header handling for PATCH is unverified (APR3: harmless for JobLens). |
| B8 | Whisper's turn-stop safety net (`ttfs_p99_latency`) is 4 s, not Pipecat's 1 s. A finalized transcript still ends the turn at once. | base.en takes 1.2–2.2 s per queued segment on this CPU. The 1 s default closed turns without their last words, and the late transcript barged in as a new turn (free e2e WER 0.74 → 0.04). |
| B9 | The Piper voice is loaded once and shared, like Whisper. | It was loaded on the event loop inside every offer (offer 3.6–7.0 s → 1.2–1.5 s; Edge missed acceptance #1 before). |
| B10 | `scripts/up-live.sh` fixed (a broken line continuation made it always fail). | Found at the live run. |

Measured (synthetic voice, laptop on battery, Balanced plan, ~40% background host load; details in
`docs/latency-acceptance.md`):

- **AS3 missed.** Run 1 browser median 3834 ms (pass) and p90 6916 ms (fail, bound 5000) over 10 gaps. The rerun was
  cut off by host memory pressure and gave server-side numbers only (median 3026, p90 3810 ms). Nothing was tuned
  (APR5). For the architect: all three slow gaps are turns where smart-turn ended the turn at a sentence pause
  mid-answer, the reply was interrupted and the LLM ran twice. The spike's data shows the same (46 interruptions over
  12 answers). Acceptance #2 ("never cut off mid-thought") is at risk on this evidence, and it needs the 2b live check
  on a real voice.
- **Acceptance #1 met** in standalone: create 105 ms + first audio 8.65 s.
- **Live spend: $0.0021** in total (two Haiku sessions; cap $0.30).
- The free spoken e2e passed 3 of 4 runs after B8/B9 (Chromium 2/3, Edge 1/1). The failure was the same mid-answer
  turn split.

## 2a acceptance ruling by the architect (2026-11-04)

Results: `docs/latency-acceptance.md`. The fix is planned in `docs/superpowers/plans/2026-11-04-interviewer-turn-fix.md`.

| # | Ruling | Why |
|---|---|---|
| AT1 | **Turn grace:** smart-turn's "complete" ends your turn only after **1.5 s** of continuous silence (`TURN_GRACE_S`). VAD still stops at 0.2 s, so Whisper starts at once and runs inside the grace. Speaking again within the grace keeps it one turn. | The miss wasn't speed: the interviewer jumped in at sentence-final pauses and talked over you, which you rank worse than a slower reply. Whisper already takes 1.4–2.2 s after you stop, so a 1.5 s grace costs almost nothing. |
| AT2 | **Resume merge:** if you start speaking within **2 s** of a turn end, the interviewer's reply is cancelled and your two fragments become one answer. That means one context message, one transcript turn, no question or follow-up advance, and one LLM run. Resumes are counted (no content) in the result's `latency.resumes`. | It is the second line of defence when a pause outlasts the grace; it also removes the double LLM run that made the slow gaps. |
| AT3 | **Acceptance #2 stays and becomes measurable:** the free spoken e2e must show 0 split answers and 0 talk-over in 4/4 runs (3 Chromium, 1 Edge). **Acceptance #3 becomes median ≤ 4.5 s, p90 ≤ 5.5 s** (browser, Piper + Haiku + base.en, 1.5 s grace). The real-voice check in 2b's live session still decides #2 for good. | You chose realism over raw speed. A real interviewer also takes 1–2 s before replying; the half-second of slack pays for never being cut off. |
| AT4 | **Rerun conditions:** laptop plugged in, Best performance power mode (you set it; the builder doesn't change system settings), other builders paused, JobLens dev containers stopped by the coordinator (prod untouched), ≥ 4 GB free memory, background CPU < 15%. Two complete runs must pass; if one fails, one more run is allowed and 2 of 3 must pass. Every run is reported. | Run 1 was on battery, Balanced, 40% background CPU and low memory, and run 2 was killed for memory. The verdict must rest on complete runs in conditions like your real use. |
| AT5 | **Not chosen:** a longer VAD `stop_secs` (it would delay Whisper and add the full wait to every reply), a minimum barge-in length, and hold-to-talk or "press Space when done". Push-to-talk stays a fallback only if your real-voice session still shows cut-offs after AT1–AT2. | A press-to-finish button breaks the realism you asked for, and the others either cost latency on every turn or don't stop the interviewer starting. |
| AT6 | Tuning is limited to one step: if the free e2e still splits at 1.5 s, all four runs are repeated once at `TURN_GRACE_S=2.0`, and that value is adopted only if they pass; otherwise the builder reports back. The builder's fixes during 2a (Whisper safety net 4 s, Piper cache) are accepted. | APR5's no-silent-tuning rule, with one pre-approved step so a near miss doesn't need another round trip. |

## Turn-fix builder's calls and results (2026-11-04)

Results: `docs/latency-acceptance.md` (turn-fix section). **AT3 met:**
- Free e2e 4/4 with 0 splits and 0 talk-over.
- Live browser median 2910.5 / 2933.5 ms and p90 3030 / 3094 ms, with 0 splits, in both runs.
- Live spend for the turn fix was $0.0021. Including 2a, the total is $0.0042 against the $0.30 cap.

| # | Call | Cost if wrong |
|---|---|---|
| TF1 | The grace counts silence from the audio frames themselves (VAD stop plus frame durations), not wall time. Smart-turn is wrapped and its stop is held until 1.5 s of silence. | If audio frames stopped arriving during silence, the count would stall. WebRTC sends frames continuously. |
| TF2 | A resume needs two things: a reply already started and not yet heard in full, and new speech within 2 s of the turn end. | A resume that comes after a very short, finished reply counts as a new answer. |
| TF3 | The director decides on a resume. It restores the interview state from that LLM run and merges the two fragments in the context. The transcript and the on-disk store drop the fragment and the cancelled reply (new `SessionStore.drop_last_turns`). | A tool call that lands after the restore would advance the interview once. |
| TF4 | Each resume costs one cancelled LLM run, because the early turn end has already started it. Avoiding that run is the grace's job. | About $0.0001 per resume. No resumes happened in any run. |
| TF5 | Fixes so the test harness behaves like the real pipeline: the fake TTS drops a stale stop, a resumed fragment interrupts, and the fake LLM replays its step on a merged re-run. | None (test-only). |
| TF6 | The e2e reloads the client page once if the esm.sh import stalls for 30 s. Vendoring the client wasn't needed. Precheck before each run: Available MBytes ≥ 4096 and CPU < 15% averaged over 30 s, after dropping the docker-desktop page cache (coordinator-approved). | None. |
| TF7 | Contract: the result gains an optional `latency.resumes` (a count, no content), plus the `TURN_GRACE_S` / `RESUME_WINDOW_S` settings (see `docs/contract-changes.md`). | None (additive). |

The live runs logged 210 and 600 ms of talk-over. That isn't a live criterion and can't be placed, because the page
events weren't saved. The 2b real-voice session should watch for it.

## 2a final review rulings (2026-11-04)

| # | Ruling | Why |
|---|---|---|
| AF1 | **Session ids are never handed out.** `GET /health` reports `busy: bool` instead of `live_session`, and `409 busy` drops `session_id`. A session id is then known only to whoever created the session (JobLens), and acts as the key to its data endpoints. Ids must be at least 128-bit random (uuid4 or `token_urlsafe(16)`), with a test. Contract change logged in `docs/contract-changes.md`; JobLens never read either field, so only a test and the fake change there. | Health was the one place that leaked the key to your transcript and recording to anything that can reach the port. Nothing uses those fields. |
| AF2 | **No shared service secret for now** (local-only accepted). The service stays bound to 127.0.0.1 plus the compose network, as Postgres is (P4). Revisit if the service is ever reachable beyond this laptop or used by a second app. | With AF1, reaching your data needs both local access and an unguessable id. A secret adds a key to every `.env` and every compose file for a threat that local access already wins (YAGNI). |
| AF3 | **Fix confirmed (Important):** a tool-only LLM re-run goes through the director like any other run. It is budget-checked against the hard cost cap and the time hard stop, and counted. After **2 consecutive** tool-only re-runs with no speech, the fixed closing is spoken without the LLM (`end_reason` `error`, error `"no_speech_loop"`). Tests: `test_tool_only_rerun_is_refused_past_the_cap`, `test_tool_only_rerun_respects_the_time_hard_stop`, `test_two_silent_reruns_force_the_closing`. | The A1/AD2 ceiling depends on the cap being hard on every path, and a model looping on tools must not spend or keep you waiting in silence. |
| AF4 | **Fix confirmed (Important):** on startup, a session that ended with `audio_state` `pending` and an intact `audio.raw` is re-encoded once in the background; a missing or unreadable raw, or a second failure, gives `audio_state` `failed`. Never left pending until the TTL. Tests: `test_recover_reencodes_pending_audio`, `test_recover_marks_audio_failed_without_raw`. | Your recording shouldn't be lost to a restart when the raw audio is still on disk, and JobLens must get a final answer, not wait 24 h. |

Fix pass for the 2a final review, by the builder (one pass, each fix test-first):
- **AF3:** `RerunGate`, after the LLM, catches the re-run that Pipecat's assistant aggregator pushes upstream. Each
  re-run then gets the director's budget check, time hard stop, notes and run count. A third consecutive silent re-run
  speaks the budget closing text (`end_reason` `error`, error `no_speech_loop`). Re-runs now count in `llm_runs`.
- **AF4:** the startup re-encode runs once only. An `audio.reencode` marker means a crash during the re-encode itself
  gives `failed` on the next start. An empty raw gives `none`, as the normal encode does.
- **AF1:** ids are `secrets.token_hex(16)`; the test checks that every one of the 128 bits varies (uuid4 fixes 6). The
  `Result.session_id` schema now has a description; the README and the plan's "Service interface" are updated.

Deferred minors from the 2a final review (not fixed; for you to decide):
- Runtime check of the aioice version (the ICE port patch assumes 0.12.1).
- `DELETE` while the audio is still pending (the encode may then write into a removed folder).
- Unauthenticated 500 on a malformed offer body; the 404 it can return is not in the contract.
- Race between the director's restore (resume merge) and a tool handler already scheduled for the cancelled reply.


## Builder's calls during implementation (release 2b, JobLens)

Branch `feat/mock-interview` in JobLens, from `feat/interview-prep`. The plan's guesses H1–H21 were built as written; the calls below are the ones made while building.

| # | Call | Why |
|---|---|---|
| BM1 | H1–H21 built as the plan states. Contract assumptions C1–C10 were checked against 2a before Task 2 and again before Task 12: routes, field names and example files matched. | Plan rule (H21). |
| BM2 | Brief strings are clipped to 2a's field limits and scrubbed with 2a's three contact patterns (email, URL, 10+ digit run) as well as Release 1's, before the final check. | So a brief JobLens sends is never refused with a 422. |
| BM3 | The client raises two errors beyond the plan's: `InvalidBrief` (422) and `SignalRefused` (401/409/410 on the offer). A `result` answered 409 (`not_finished`) is treated as "down": the row stays ended and the next reconcile retries. | Each is mapped to a message the page can show, and nothing is lost. |
| BM4 | If no master-CV paragraph has 25 or more words, the candidate summary falls back to the longest paragraph. | The plan's own test text needed it. |
| BM5 | Service health `error` shows as "down", with "The interviewer isn't set up: …". A starting row that the service reports live gets `connected_at`. | JobLens shows only ready, loading or down; the usage estimate (H4) depends on `connected_at`. |
| BM6 | A session the service failed that never connected ends `failed` ("The interview never connected, so nothing was recorded or spent"), not `nothing_to_rate`. Usage is recorded under a row lock. | It was never an interview, and nothing was spent. |
| BM7 | The rating prompt lists only the main questions that were answered; the rest are stored as "not asked". | The model never rates a question that wasn't asked. |
| BM8 | Start is also greyed out while another session is live, and the "at most" figure is rounded to cents. | The page shouldn't offer a Start the server would refuse. |
| BM9 | The job page keeps checking the interviewer every 5 s while it is loading, so the line turns ready without a reload. | The plan's browser test expects it; it costs one GET every 5 s. |
| BM10 | The live test opens its own browser with the session WAV as the microphone. The service's latency figures are read from the dev database before the session is deleted. | Playwright allows that browser option only once per file. The API doesn't expose latency. |
| BM11 | Contract changes followed. AT2: `latency.resumes` is accepted (optional, default 0) and stored. AF1: a 409 busy reply carries no session id; the busy error no longer names one. AF4 (the service's audio `failed`) needed no change: the audio route answers 404, so JobLens marks the recording lost. Each change has a test. | `docs/contract-changes.md`. |
| BM12 | Dev had no Interviewing application, so BR8 was repeated for this run. After a dev backup, the builder moved dev's one Submitted job to Interviewing by SQL and put it back afterwards (status and `updated_at`). Prod was not touched. | Same as BR8 (APR9). |
| BM13 | **Live dev run, two runs, $0.26 in total:** prep $0.165, rating $0.090, interviewer LLM $0.002 (Haiku). Run 1 failed only acceptance #1, with the first question at 13.5 s. It was the first session after a cold interviewer start and a page-cache drop on a memory-tight laptop: about 8 s passed before the browser sent the offer, and the offer took 2.8 s on the server. Run 2 passed with the first question at 4.4 s (offer sent at 1.0 s, answered at 2.1 s). Both runs were rated, **Play from here** appeared, and everything made was deleted. Service reply latency: median 2630 / 2724 ms, p90 2717 / 2871 ms, `resumes` 0. | For you to judge acceptance #1. The first session after a cold start on a loaded machine can exceed 10 s. |

## Final review (Release 2b, 2026-11-04)

No Critical findings. Two were fixed, each with a test that failed first. Backend suite after the fixes: 688 passed, 1 skipped; web tsc clean, vitest 92/92.

| # | Fix | Test |
|---|---|---|
| FR3 | A session could stay at `ended` or `rating` until a restart. That happened when collection hit a down interviewer (only startup recovery retried it), when the result was unreadable, or when rating raised an unexpected error. The page then showed "collecting" for ever, Delete was refused, and prod's idle check blocked `prod.ps1 up`. Now an `ended` row is collected again by the session page's poll and by the job page's list, at most once at a time per session. An unreadable result or an unexpected collection error moves the row to **failed** with a clear message: the connected session's usage is estimated at the cap (AD4), whatever was saved stays, and the recording is still fetched. An unexpected rating error gives **Rating failed** with **Retry rating**. | `test_a_transient_down_during_collect_is_retried_by_reconcile`, `test_the_job_page_list_collects_an_ended_row`, `test_an_invalid_result_fails_the_session_and_keeps_the_recording`, `test_a_rating_exception_gives_rating_failed` |
| FR4 | The rating's fact check took a My CV **question** as evidence, the same bug as FR1 in prep. Now only the answers' own words count, plus what you said in the session. | `test_rating_my_cv_question_is_not_evidence` |

Deferred minors (not fixed; for you to decide):

| # | Minor |
|---|---|
| DM6 | If creating a session times out, JobLens deletes its row while the service may still hold that session. Start then gets "busy" until the service drops the unconnected session (120 s). |
| DM7 | A crash in the middle of recording a session's usage records those rows again on retry: spend is overcounted (the safe side). |
| DM8 | The transcript in the rating prompt isn't passed through the contact scrub. The interviewer's model already heard it live. |
| DM9 | `prod.ps1 up` appends the missing `.env.prod` keys before the idle check (harmless). |
| DM10 | A session that failed without ever connecting doesn't delete the service's copy; the service's TTL cleans it up. |
| DM11 | **Retry rating** checks the daily budget, not the room left under the hard cap. It is one call at most. |

## Interview UX + audio check: design approval by the architect (2026-11-04)

Design: `docs/superpowers/specs/2026-11-04-interview-ux-and-audio-check-design.md`. Approved with changes.

### Rupak (2026-11-04, after the first dev sessions)

| # | Decision |
|---|---|
| R11 | A table of questions with buttons such as Review, Accept and Reject, so browsing is easy. |
| R12 | After Start mock interview and the mic permission, a page that tests audio both ways (you hear the interviewer, the interviewer hears you); only once it passes does the real interview start. |
| R13 | Make the UI more user friendly: the job page is getting crowded. |
| R14 | You'd rather not wear a headset. |

### Rulings and changes

| # | Change or ruling | Why |
|---|---|---|
| UX1 | **Status model approved** (`to_review`/`accepted`/`rejected`, accepted first, then to_review in order as the fallback), with two changes. (a) Old sets map at read time with no data rewrite: unticked → rejected, own → accepted, and other ticked → **to_review**, not accepted. (b) The Start panel states the mix: "Uses 5 accepted + 3 not yet reviewed questions". | Release 1 ticked everything by default, so calling those "accepted" would claim a review you never did. The fallback keeps the start one click; the line keeps it honest. |
| UX2 | **Skip check** only for an **explicitly chosen** mic (a real device id, never "Default") that passed in this browser within 24 h. The chosen mic is remembered in the browser and preselected. | "Default" is exactly what silently moved to a virtual device on 4 Nov. A remembered real device makes the second interview a one-click start. |
| UX3 | The "can't hear you" banner fires on `silent_input` only; the 12 s no-speech trigger is dropped. | Thinking quietly in a real room still sends noise. A banner after 12 s of thinking would nag you mid-interview, while digital silence is the actual fault. |
| UX4 | Prepare runs in the background (202, then polling); the in-flight state stays in the app process and clears on restart; a second click gets 409. | 3.5 minutes is too long to hold a request open; nothing needs a table for a state that dies with the process (YAGNI). |
| UX5 | No headset approved (echo cancellation, noise suppression, auto gain). If the live test shows self-interruption, the one pre-approved fallback is `BARGE_IN_MIN_S=0.8` for no-headset use, then a re-test. This narrows AT5's "no minimum barge-in" to the headset case. | R14. The interviewer hearing itself through the speakers is the main risk, and a short barge-in floor is the least visible fix. |
| UX6 | Three tabs approved (Application / Interview prep / Mock interviews), plus a **Start mock interview** button in the Interview prep tab's header. | Less crowding (R13) without adding a click between reviewing and rehearsing. |
| UX7 | The **service check mode is approved over a client-only check**: no LLM, $0, same lock and token flow. | You asked that the *interviewer* hears you and you hear it (R12). Only an end-to-end check covers the WebRTC path, UDP ports, STT and TTS; a local meter would pass while the call fails. |

## Interview UX + audio check: plan approval by the architect (2026-11-04)

Plans: IAssistPro `docs/superpowers/plans/2026-11-04-audio-check.md`, JobLens `docs/superpowers/plans/2026-11-04-interview-ux.md`. Both approved; JobLens with contract edits.

| # | Change or ruling | Why |
|---|---|---|
| UXP1 | **Contract reconciled; the service plan is the source of truth.** JobLens's K1–K9 were edited to match it. The Play again client message is **`play_prompt`** (not `play_again`). `POST /checks/{id}/end` returns 202 only after the lock is free (waits up to 5 s; JobLens's client allows 10 s). `silent_input` carries `reason` (`zeros`/`no_audio`), with one copy for both. JobLens handles `check_ended` with "The check timed out. Start it again." and a **Start again** button. Also: 404 `unknown_check` → `CHECK_GONE`; 409 `wrong_state` under `TRANSPORT=websocket`; one offer per check, so a mic change on the check page means end + new check (the live screen switches by `replaceTrack`). | The two plans must meet on the same wire names before either is built. |
| UXP2 | The service plan's additions to §3.1 are accepted: `play_prompt`, `GET /checks/{id}`, PATCH offer, `check_ended`, `silent_input` on no frames with `reason`, end waiting for the lock, SmallWebRTC-only checks, and mic switching by `replaceTrack`. Silence means exact-zero samples (or no frames). | Each closes a hole the design left (Play again, a muted track sending nothing, Begin hitting `busy`). Exact zero keeps a quiet room from ever triggering it (UX3). |
| UXP3 | **`BARGE_IN_MIN_S` is built now and stays default 0**; JobLens's compose doesn't set it. Your no-headset session (13 turns, follow-ups fine, median 2.7 s, no self-interruption) passes on that front. The one 0.9 s turn after a question is ambiguous: the floor wouldn't catch it anyway, since it applies only while the interviewer speaks. If you confirm it was echo, the next step is a narrow "echo tail" guard (drop a < 1 s candidate segment starting within 300 ms of the interviewer stopping), as its own small change with a test. Nothing changes on one unconfirmed event. | Evidence first. A floor > 0 would make real interruptions sluggish to fix a problem not yet seen, and it wouldn't touch this case. |
| UXP4 | JobLens guesses confirmed. G1: status stored per question, `ticked` derived, no migration. G2: the chosen questions are asked in table order, so "Tell me about yourself" stays first. G3/G4: checks are global and held in memory, and starting a session ends open checks. G5: no "Default" mic entry. G7: a separate prep runner. G9: jobs open on Application. G10: meters show what the interviewer receives. G12: pass needs both steps. G16: `keepalive` end. The Task 6 fix (open the chosen mic before connecting, so it can't race Daily's device pick) is approved, with the decided fallback. | They match R11–R14 and UX1–UX7. The mic race is the most likely cause of your silent Chrome besides the setting you fixed, so it gets a test. |
| UXP5 | Other guesses (G6, G8, G11, G13–G15) accepted as written. | Small, reversible, consistent with the design. |
