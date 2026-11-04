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
