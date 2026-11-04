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
