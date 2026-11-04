# Latency acceptance (AS3), release 2a and turn fix

Date: 2026-11-04. Branch `feat/mock-interviewer`. Measured with `e2e/spoken.e2e.ts` (live test) and summarised by
`scripts/latency_report.py`. No transcript text appears here.

## Turn fix (rulings AT1–AT6), re-run 2026-11-04

### Verdict

**AT3 met.** Turn grace is 1.5 s (AT1) and the resume merge window is 2 s (AT2). Defaults were unchanged and AT6's
2.0 s retry wasn't needed.
- Acceptance #2: the free spoken e2e passed 4/4 runs (3 Chromium, 1 Edge) with 0 split answers and 0 talk-over.
- Acceptance #3: both live runs passed, so no third run was needed.
  - Browser median 2910.5 / 2933.5 ms against a bound of 4500.
  - p90 3030 / 3094 ms against a bound of 5500.
  - 0 splits.

### Conditions (AT4)

- Plugged in, Best performance (set by Rupak), JobLens dev containers stopped and other builders paused (by the
  coordinator). Only the JobLens prod containers were running; they were untouched and idle.
- Before each run, the docker-desktop WSL page cache was dropped, then the run was gated on two checks:
  - Available MBytes ≥ 4096.
  - Host CPU average < 15%, sampled every 2 s over 30 s.
- Runs were one at a time, with one browser each, closed before the next run.
- Image `sha256:a2baa5a4132e…`. Otherwise the configuration is the same as the 2a run below; the live brief and fake
  microphone are unchanged.

### Free spoken e2e (fake LLM, AT3 gate)

| Run | Precheck (free MB / CPU avg) | Splits | Talk-over | LLM runs | Resumes | WER |
|---|---|---|---|---|---|---|
| chromium3 | 4660 / 9.7% | 0 | 0 ms | 7 | 0 | 0.059 |
| chromium4 | 5170 / 11.1% | 0 | 0 ms | 7 | 0 | ≤ 0.059 |
| chromium5 | 5387 / 7.4% | 0 | 0 ms | 7 | 0 | ≤ 0.059 |
| msedge2 | 5226 / 13.7% | 0 | 0 ms | 7 | 0 | ≤ 0.059 |

All four runs had a WER between 0.037 and 0.059. Seven LLM runs means the greeting plus 6 answers, with no double run
for any answer. Browser gaps were 1.8–2.2 s, measured with the fake LLM. Earlier attempts that don't count:
- chromium1 failed on a harness bug: it read `llm_runs` from stdout, but docker logs go to stderr. Even so, it had 0
  splits and 0 talk-over.
- chromium2's page never loaded because the esm.sh client import stalled. The harness now reloads the page once.
- msedge1 passed, but its precheck CPU was 24.2%.
- One rerun batch was stopped by the host for low memory.

### Live runs (Haiku, $0.05 brief cap each)

| Run | Precheck (free MB / CPU avg, max) | Browser n | Browser median / p90 | Server median / p90 | Splits | Talk-over | LLM runs | Resumes | First audio | Cache-read tokens | Cost | End |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 5341 / 14.5% | 12 | 2910.5 / 3030 ms | 2580 / 2699 ms | 0 | 210 ms | 13 | 0 | 5493 ms | 32,896 | $0.0012 | ended by candidate |
| 2 | 5477 / 13.5%, 26% | 10 | 2933.5 / 3094 ms | 2582 / 2702 ms | 0 | 600 ms | 11 | 0 | 3421 ms | 29,174 | $0.0009 | time limit |

- Each run's LLM count is the greeting plus one run per answer: no double runs, unlike 2a's three.
- Talk-over is only a gate in the free e2e. The live runs did log some (210 and 600 ms), but the page's events weren't
  saved, so they can't be placed. One possibility is the interviewer's closing or greeting overlapping a clip that
  started on the fixed 14 s schedule (Haiku replies are longer than the fake LLM's). That is unconfirmed, and the 2b
  real-voice session should check it.
- Interviewer container, from `docker stats` every 2 s (100% = 1 core):
  - Run 1: 92 samples, average 82%, peak 559%, memory peak 1077 MiB.
  - Run 2: 78 samples, average 102%, peak 571%, memory peak 1248 MiB.
- Live spend for the turn fix: **$0.0021**. Including 2a's $0.0021, the total is **$0.0042** against the $0.30 cap.

`scripts/latency_report.py` now uses the AT3 bounds (4500 / 5500 ms). It fails a run with any split and prints
splits, talk-over, LLM runs and resumes.

---

# Release 2a run (superseded by the turn fix above)

## Verdict

**AS3 not met.** Run 1: browser median **3834 ms** (bound 4000, pass) and p90 **6916 ms** (bound 5000, **fail**)
over 10 gaps. The one allowed rerun (run 2) was cut off by host memory pressure at ~243 s, before the browser
reported its gaps, so it gives server-side numbers only (median 3026 ms, p90 3810 ms over 10 replies). Those are not
the AS3 measure and do not count as a pass. As APR5 requires, nothing was tuned and the defaults are unchanged. This
miss goes to the architect with the breakdown below.

**Acceptance #1 met** in this setup. Session create took 105 ms, and the first interviewer audio came 8.65 s after page
load (Haiku greeting, warm models), for 8.76 s in total against a bound of 10 s.

## Breakdown of the miss

Run 1 gaps (ms, browser, end of candidate speech → first interviewer audio):
3267, 3741, **11330**, **6916**, 4464, 3724, 3242, 3843, 3825, **5412**.

The three slow gaps are exactly the turns where the server made **two** LLM runs for one answer. The LLM run count
jumps by 2 at 05:08:38, 05:11:14 and 05:11:54. Smart-turn v3 judges a sentence-final pause inside an answer
"complete". If Whisper finalizes that short segment before the candidate resumes, the turn ends mid-answer. The
interviewer then starts a reply and is interrupted. The rest of the answer becomes a new user turn and needs a second
LLM round trip. In the turns without a split, every gap is 3.2–4.5 s.

The same effect is in the spike's own data: 46 interruptions over 12 answers, and the timer kept only each answer's
last fragment. The spike's "no premature cut-offs" finding therefore doesn't hold for these synthetic clips. Whether a
real voice shows it as often (continuation intonation) is the 2b live check.

Possible routes, none tried here: an interruption guard such as a minimum word count before a resumed candidate turn
interrupts, a longer VAD `stop_secs` or smart-turn threshold, or merging a continuation into the interrupted turn.
These are the architect's call.

## Runs

| Run | Browser gaps n | Browser median / p90 | Server n | Server median / p90 | Create + first audio | Cache-read tokens | Cost |
|---|---|---|---|---|---|---|---|
| 1 | 10 | 3834 / 6916 ms | 12 | 3318 / 9654 ms | 105 ms + 8650 ms | 33,410 | $0.0012 |
| 2 (cut off at ~243 s) | – | – | 10 | 3026 / 3810 ms | – | 28,679 | $0.0008 |

Total live spend in this release: **$0.0021** (both runs, at the brief's Haiku prices). The cap was $0.30.

## Configuration

- Image `sha256:f0d977f2607c…` (runtime target). Pipecat 1.15.0, aiortc 1.15.0, aioice 0.12.1 (patched),
  faster-whisper 1.2.1, piper-tts 1.8.0, onnxruntime 1.31.1.
- Defaults: SmallWebRTC; Whisper `base.en` int8, beam 1, 4 threads (turn-stop safety net 4 s); Piper
  `en_US-lessac-medium`; Silero VAD `stop_secs` 0.2 + smart-turn v3; Claude `claude-haiku-5-5`, `max_tokens` 200,
  prompt caching on.
- Brief `e2e/fixtures/brief.live.json`: 8 questions, 7 minutes, $0.05 cap. Fake microphone `e2e/out/session_live.wav`:
  12 Kokoro `am_michael` clips, 25 s lead, 14 s gaps, 284.7 s. Chromium 1194 headless, `/end` after 300 s.

## Machine

- Laptop i5-1235U (2P + 8E cores) on **battery** (85%), Windows power plan **Balanced**, Docker Desktop / WSL2 with
  12 vCPU and 7.6 GiB.
- Background host CPU was about 40% before the run (desktop apps and other agent sessions; the JobLens containers were
  idle). Host memory was tight: run 2 was stopped because free memory dropped below the host tool's limit
  (0.87 GB free of 15.7 GB afterwards). The spike ran on a quieter machine.
- Interviewer container (docker stats every 2 s, 100% = 1 core). Run 1: 89 samples, CPU average 166%, peak 695%,
  memory peak 1169 MiB. Run 2: 63 samples, average 215%, peak 655%, memory peak 1261 MiB.

## Notes

- The voice is synthetic only. The AS5 re-check on Rupak's own voice happens in the 2b live dev session, local only
  and never committed.
- Two fixes from the free e2e (Task 13) are in this run: Whisper's turn-stop safety net raised to 4 s (Pipecat's 1 s
  default cut answers short), and the Piper voice is cached (offer handling went from 3.6–7.0 s to 1.2–1.5 s).
