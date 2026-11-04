# Interview UX and audio check: design

Date: 2026-11-04. Owner: Rupak. Builds on `2026-11-04-mock-interviewer-design.md` (released to dev from `main`:
JobLens da02212, IAssistPro 8676457). Repos: IAssistPro (audio check mode) and JobLens (UI).

## 1. Why (what Rupak hit on dev, 4 Nov)

- **No audio reached the interviewer.** Rupak's Chrome sent pure digital silence in two sessions (recording RMS exactly
  0.0 after the interviewer's greeting). Playwright Chromium on the same laptop with the same real Intel microphone
  sent room noise and was heard. Most likely cause: Chrome's own microphone setting points at a virtual device
  (the laptop has "CABLE Output (VB-Audio Virtual Cable)" and "Stereo Mix", which output silence), and JobLens's
  "Default microphone" follows Chrome's choice. The interviewer just waited; nothing told Rupak.
- The mic picker appears only before the first session; afterwards there was no way to change it.
- **Prepare questions took ~3.5 minutes** while the page said "about 30 seconds"; leaving and returning lost the
  "Preparing…" state.
- **Mock interview was a pain to reach**: the Interview prep section is a long scrolling list; the page is crowded.
- Rupak prefers **not wearing a headset**.

Rupak's asks (verbatim intent):
1. "a table of questions with buttons like review, accept, reject etc. that way browsing is easy".
2. "when clicked Start Mock Interview after permission have a page to test audio both sides as mic testing as well
   whether interviewer is able to speak which I can hear and other way round. once this passes go to real interview."
3. "make the UI more user friendly given that that page is getting occupied".

## 2. Goals / non-goals / anti-goals

Goals
- G1. Question **table**: one row per question; Review / Accept / Reject per row; easy browsing.
- G2. **Audio check** before every mock interview, end to end through the real interviewer (browser → service →
  browser), both directions, with a mic picker on the same screen. Begin interview only after it passes.
- G3. A **less crowded** job page: interview work in its own tab; a clean live screen.
- G4. Fixes: "we can't hear you" during a session; works **without a headset**; honest prep wait time and a
  preparing state that survives navigation; mic changeable any time.

Non-goals: new rating features, video, prod release (separate ask), paid speech.

Anti-goals: an audio check that spends money (it must use no LLM); an extra step that can be failed by a quiet room
or a short answer (the check must be forgiving and fast: target under 30 seconds); losing the existing data (question
sets, ticks, own questions, sessions).

## 3. Audio check (IAssistPro + JobLens)

### 3.1 Service: a check session (IAssistPro)

- `POST /checks` (no brief needed; same one-session lock as interviews; 409 `busy` if a session is waiting/live) →
  `{check_id, connect}`; same SmallWebRTC offer flow and one-time token as sessions.
- Pipeline: transport → VAD → STT (same configured STT) ; TTS (same configured TTS). **No LLM.**
- Script:
  1. Service speaks a fixed sentence: "This is your interviewer. If you can hear me, press 'I heard it'." (TTS)
  2. Service streams **inbound mic level** to the browser (RTVI server message, ~5/s: `{"type":"mic_level","rms":…}`),
     plus `{"type":"silent_input"}` if ≥3 s of the call's inbound audio is digital silence (all-zero samples).
  3. On the browser's request ("now listen", RTVI client message) the service transcribes the candidate's next turn
     (VAD + same turn rules) and sends `{"type":"heard","text":"…"}`; the candidate can repeat.
- Ends on `POST /checks/{id}/end` or disconnect or 2 minutes; nothing stored, no recording, no usage (cost $0).
- Logs: no transcript text (same canary rule).

### 3.2 JobLens: the check screen

- Start mock interview opens the **Audio check** screen (full width): permission prompt → connect a check session
  (proxied like the interview offer).
- Step 1 "Can you hear the interviewer?": plays the sentence; buttons **I heard it** / **Play again**; help text if
  not (speaker volume, output device).
- Step 2 "Can the interviewer hear you?": live level bar fed by the service's `mic_level` (what the interviewer
  receives, not the local mic); prompt "Say: 'I am ready for my interview'" → shows "The interviewer heard: …".
  Passes when any non-empty text is heard. If `silent_input` arrives: "The interviewer is receiving silence from
  '<mic label>'. Pick another microphone below, or check Chrome's microphone setting (chrome://settings/content/
  microphone)." Virtual devices (labels containing "CABLE", "VB-Audio", "Stereo Mix", "Virtual") are listed last
  and marked "(virtual — usually silent)".
- Mic picker on this screen; changing it reconnects the check. The chosen device id is used for the interview.
- When both pass: **Begin interview (~$x, at most $y)** → ends the check, starts the session (existing flow) using
  the same mic. "Skip check" is offered only after a check has passed in this browser in the last 24 h for the same
  **explicitly chosen** mic (a real device id, never "Default", which can change underneath as it did on 4 Nov). The
  chosen device id is remembered in this browser (localStorage) and preselected (UX2).

### 3.3 During the interview

- The live screen shows the interviewer-side mic level (same `mic_level` messages, sent by interview sessions too).
- If `silent_input` arrives, a banner: "The interviewer can't hear you" with a mic switcher (switching re-negotiates
  the mic track). **No banner for a long silence with a live level** (room noise arriving): that is you thinking, and
  the interviewer must not nag (UX3).
- **No headset**: request `echoCancellation`, `noiseSuppression`, `autoGainControl` true on the mic; remove the
  "Use headphones" instruction (keep a tip: "If the interviewer stops when you didn't speak, use headphones").
  Verify in a live test that the interviewer's own voice from the laptop speakers does not trigger a candidate turn
  or barge-in. If it does, the one pre-approved fallback is a service setting `BARGE_IN_MIN_S` (default 0 = today):
  the interviewer stops only after that much candidate speech, set to 0.8 s for no-headset use and re-tested (UX5).

## 4. JobLens UI

### 4.1 Job page tabs
- Tabs on the job page: **Application** (everything that is there today), **Interview prep** (question table),
  **Mock interviews** (start, sessions list, results). The tabs appear when status is Interviewing or later, or when
  prep data exists; the URL carries the tab (`/jobs/:id?tab=prep`). The status change to Interviewing switches to
  the Interview prep tab with a one-line hint.

### 4.2 Question table (Interview prep tab)
- Header: CV used, date, cost, **Prepare / Regenerate (~$x)**, counts ("9 accepted · 2 rejected · 3 to review"),
  filter chips (All / To review / Accepted / Rejected, and by kind), **Copy accepted questions**.
- Columns: #, kind (chip), question (one line, truncated), status, actions: **Review** (expands the row inline:
  why they ask, what a strong answer covers with [CHECK] marks and sources), **Accept**, **Reject**, move up/down.
- Status model: each question is `to_review` (new), `accepted`, or `rejected`. Accepted = ticked (used by the mock,
  in table order); rejected = unticked. Existing sets are mapped **at read time, with no data rewrite**: unticked →
  rejected, own → accepted, other ticked → **to_review** (Release 1 ticked everything by default, so a tick was never
  a review; UX1). A freshly generated set starts **all to_review**. The mock uses accepted questions, falling back to
  to_review ones in order when fewer than the mock's question count are accepted, so Rupak can start without
  reviewing everything. The Start panel says so: "Uses 5 accepted + 3 not yet reviewed questions" (UX1).
- Own question: "+ Add your own question" row at the bottom (accepted on add).
- Saves as today (autosave per change, stale-set 409 handling kept).
- Prepare: the button text and note say "This takes 1–3 minutes". **Prepare runs in the background** (POST returns
  202, the prep GET is polled), so no request is held open for minutes (UX4). The preparing state is kept in the
  app process per job (`preparing_since` on the prep GET, cleared in `finally`; a restart clears it because the call
  died with it), so leaving and returning shows "Preparing… (started 1 min ago)". A second click is refused (409
  "already preparing") instead of paying twice. A failure shows its message on the tab on the next poll.

### 4.3 Mock interviews tab
- Start mock interview (cost, ceiling) → audio check screen (§3.2) → live screen → results. The same **Start mock
  interview** button also sits in the Interview prep tab's header, so reviewing and rehearsing are one click apart
  (UX6).
- Past sessions as a table: date, length, questions answered, three average scores, status, open / delete.

## 5. Constraints (unchanged)
Everything in the base design §3 and the decisions log: no spend without a click showing cost; no invented facts;
privacy (no email/URLs/Leads); prod untouched; nothing pushed/merged/tagged without Rupak; commits with the
Co-Authored-By trailer; e2e never against prod; IAssistPro may be public (no personal data).

## 6. Testing
- IAssistPro: check session unit tests (no LLM constructed; lock; token; 2-minute end; silent-input detection on
  all-zero frames; mic_level messages; heard text; no transcript in logs); spoken e2e of the check with the fake mic
  (passes) and with a silent fake input (silent_input sent).
- JobLens: question status model and mapping (old sets), table actions, filters, prepare in-flight state and 409,
  tabs; check screen states with a fake voice hook (Playwright, mocked API), including silent_input and mic switch;
  can't-hear banner; a live dev run of the check (free) and one short interview without a headset (≤ $0.30).

## 7. Acceptance
1. With Chrome's mic set to a virtual device, the check shows the silent-input message within 5 s; switching to the
   Intel mic in the picker makes the interviewer hear "I am ready for my interview".
2. Check passes in under 30 s for a normal user; costs $0.
3. A no-headset interview on laptop speakers: no self-interruption across ≥ 5 interviewer turns.
4. Rupak can see all questions at a glance and accept/reject with one click each; the job page's Application tab
   looks as it does today.
5. Prepare survives navigation and never pays twice for one click sequence.
