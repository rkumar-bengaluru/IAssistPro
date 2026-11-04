# Spike: transport, local speech and reply latency

Date: 2026-11-04. Builder spike for §5.2–5.3 and §10 of
`docs/superpowers/specs/2026-11-04-mock-interviewer-design.md`. Code: `spike/` (throwaway, kept for reference).
All audio is an invented candidate ("Sam") voiced by Kokoro; no personal data.

## TL;DR

| Question | Answer |
|---|---|
| Transport | **SmallWebRTC works through Docker Desktop** from Chrome 156, Edge 156 and Playwright Chromium 141, first try, 14/14 connects, ICE completes in ~50 ms. It needs one ~30-line aioice patch (fixed UDP port range + advertise `127.0.0.1`) and `-p 127.0.0.1:40000-40009:40000-40009/udp`. The WebSocket transport also works and gives the same server-side latency. **Pick: SmallWebRTC** (design's pick rule). |
| Whisper model | **`base.en`, 4 threads, beam 1.** `small.en` / `distil-small.en` cost 2–2.5× more time (3–3.6 s for each utterance) for no accuracy gain on these clips. "Threads = all" is **slower** on this CPU: 12 threads is 2× slower than 2–4. |
| Turn detection | **Silero VAD (stop 0.2 s) + local smart-turn v3 (ONNX, CPU, ~230 ms)**. It ended 12/12 answers correctly across 64 mid-answer pauses and never cut the candidate off. It decides sooner than a 0.8 s silence threshold. Needs no torch. |
| TTS | Kokoro is the latency problem on this CPU: time to first audio is **1.0–3.5 s** for each first sentence (real-time factor ~0.8). **Piper** (`pipecat-ai[piper]`, `en_US-lessac-medium`) is **0.19 s** (real-time factor 0.08). |
| Reply latency (median / p90, end of candidate speech → first agent audio, measured in the browser) | Kokoro + Haiku: **5.1 s / 6.7 s**. Kokoro + Sonnet: **7.9 s / 9.0 s**. **Piper + Haiku: 3.9 s / 4.0 s**. Piper + Sonnet: 5.9 s / 7.6 s. **No configuration meets the 3.5 s target**; the best gets within 0.4 s. |
| Interviewer LLM default | **`claude-haiku-5-5`** for the live interviewer if latency matters most. Sonnet adds ~2 s at the median. Sonnet must run with `thinking: between_tools` (Pipecat's default for it); with adaptive thinking it took 2.2 s to the first token and spent all 150 output tokens on thinking. The architect decides (see Recommendation). |
| Recording | `AudioBufferProcessor(num_channels=2, sample_rate=16000)` captures both sides, time-aligned: user on the left channel, bot on the right, plus separate tracks. Raw output is 16-bit PCM WAV (~3.7 MB/min stereo). `soundfile` (already a Pipecat dependency) writes mono **Ogg/Opus** at ~0.14 MB/min with no extra dependency. |
| Resources | Container: ~1.1–1.2 GiB RSS, CPU averages 2.6–2.9 of 12 cores during a session, peaking at 6–9 cores (Piper runs average ~1.4 cores). The JobLens containers were not touched. |

## Versions

| Component | Version |
|---|---|
| pipecat-ai (PyPI latest stable, uploaded 2026-10-29) | **1.15.0** with extras `anthropic,kokoro,piper,silero,whisper,webrtc,websocket,runner` |
| faster-whisper / ctranslate2 | 1.2.1 / 4.8.5 (CPU int8) |
| kokoro-onnx / onnxruntime | 0.6.1 / 1.31.1 (models `kokoro-v1.0.onnx`, `kokoro-v1.0.int8.onnx`, voice `af_heart`) |
| piper-tts | 1.8.0 (voice `en_US-lessac-medium`) |
| aiortc / aioice | 1.15.0 / 0.12.1 |
| anthropic (Python) | 1.17.0 |
| JS: @pipecat-ai/client-js, small-webrtc-transport, websocket-transport | 1.16.0, 1.11.2, 1.8.2 (from esm.sh in the spike page) |
| Container | python:3.12-slim (3.12.16), Docker Engine 29.9.2 (Docker Desktop, WSL2, 12 CPUs, 7.6 GB) |
| Browsers | Playwright 1.56.1 Chromium 141 (headless), Chrome 156, Edge 156 |

## 1. Transport

**SmallWebRTC through Docker Desktop: works with a small fixed config.**

aiortc/aioice binds host candidates on random UDP ports on the container's private IP (172.17.x), which the Windows
browser can't reach, and aioice 0.12 has no port-range option. The spike replaces
`aioice.ice.Connection.get_component_candidates` (see `patch_aioice` in `spike/bot.py`). The patch:

- binds `0.0.0.0:<first free port in 40000–40009>` (one port for each peer connection, because the transport uses
  max-bundle);
- advertises a single host candidate `127.0.0.1:<port>`.

Container config:

```
-p 127.0.0.1:8765:8765                         # HTTP: /api/offer (signalling), page
-p 127.0.0.1:40000-40009:40000-40009/udp       # WebRTC media, one port for each live connection
ICE_PORTS=40000-40009  ICE_HOST=127.0.0.1      # spike env read by patch_aioice
```

No STUN or TURN is needed. The browser's own candidates are mDNS `.local` names that the container can't resolve.
That doesn't matter: the browser sends checks to `127.0.0.1:40000`, Docker Desktop's port forwarder relays them,
and aioice answers the peer-reflexive address it sees. Results:

- Chromium 141 headless: 6 SmallWebRTC sessions (5 full 5-minute sessions + 1 short), all stable.
- Chrome 156 and Edge 156 (real channels, headless): 1 session of 45 s each, then 6 alternating reconnects.
  **14/14 SmallWebRTC connects succeeded in total.** ICE completed in ~50 ms. `connect()` resolved in 1.8–3.3 s (most of that is
  `waitForICEGathering` on the browser side). Port 40000 was reused cleanly after each disconnect.
- Fake-mic audio arrived intact. Whisper transcripts matched the scripted answers.

**WebSocket** (`FastAPIWebsocketTransport` + `ProtobufFrameSerializer` ↔ JS `WebSocketTransport` +
`ProtobufFrameSerializer`) also worked first time: connect in 0.9 s, server-side reply latency identical to
SmallWebRTC (4.6 s median in the same configuration). The WebSocket client plays bot audio through its own player
and exposes no bot `MediaStreamTrack`, so the browser-side gap couldn't be measured for it.

**Pick: SmallWebRTC.** It connected reliably from Chrome and Edge with a small fixed config, and it brings
the browser's echo cancellation and jitter buffer. Costs for the real build:

- It depends on a monkeypatch of aioice internals. Pin aioice and add a test that fails if
  `get_component_candidates` changes. Upstream alternative: aioice has no port-range option; contributing one would
  remove the patch.
- One UDP port per live session. With "one live session at a time", 2–3 ports are enough (allow for a reconnect
  overlapping the old connection). Compose needs `127.0.0.1:<range>:<range>/udp`, and dev and prod need different
  ranges.
- JobLens proxying the offer (§5.3) is plain HTTP POST/PATCH of SDP JSON. Media doesn't go through JobLens.

## 2. STT: faster-whisper, CPU int8

12 spoken answers (3.6–10.4 s, median 8 s), clean and with white noise at 20 dB SNR. Word error rate (WER) is
against the script after simple normalisation (numbers as words or digits). Load time is with the model already in
the volume.

**Thread count matters more than model size.** One 8.2 s utterance, beam 1, best of 3 runs:

| threads | base.en | small.en |
|---|---|---|
| 1 | – | 4.0 s |
| 2 | **0.83 s** | 3.2 s |
| 3 | – | 3.5 s |
| 4 | 0.89 s | **2.9 s** |
| 6 | 1.37 s | – |
| 8 | 1.43 s | – |
| 12 (all) | 2.07 s | 5.2 s |

The i5-1235U is 2 performance cores and 8 efficiency cores, behind a WSL2 VM. CTranslate2 with all 12 threads waits
on the slowest threads. **Use 2–4 threads, not "all"** (this departs from the design's "threads = all"). Run-to-run
spread on this laptop is wide (±30%).

Full comparison at 4 threads (`spike/out/bench_stt.json`):

| model | beam | WER clean | WER noisy | median time / utterance | max | real-time factor | RSS | load |
|---|---|---|---|---|---|---|---|---|
| **base.en** | 1 | 1.6% | 3.0% | **1.54 s** | 1.93 s | 0.19 | 0.5 GB | 1.5 s |
| base.en | 5 | 1.6% | 2.6% | 1.54 s | 3.10 s | 0.21 | 0.5 GB | |
| small.en | 1 | 1.6% | 2.3% | 3.64 s | 4.24 s | 0.46 | 1.4 GB | 1.9 s |
| small.en | 5 | 1.6% | 2.3% | 3.82 s | 4.77 s | 0.49 | 1.4 GB | |
| distil-small.en | 1 | 3.8% | 3.0% | 2.93 s | 3.46 s | 0.39 | 1.1 GB | |
| distil-small.en | 5 | 3.0% | 3.0% | 3.10 s | 3.46 s | 0.40 | 1.1 GB | |

Whisper always encodes a 30 s window, so time barely depends on utterance length. The encoder cost sets the floor
for each model. Inside the live pipeline, base.en took 1.35–1.42 s for each turn. Pipecat's `WhisperSTTService` uses
faster-whisper's default beam 5 and doesn't pass `cpu_threads`. The spike overrides both (`WhisperBeam` and
the cached model factory in `spike/bot.py`).

**Choice: `base.en`, beam 1, 4 threads.** Caveat: synthetic TTS speech is easier than a real voice. Rupak's own
voice may need `small.en`, which costs ~2 s more for each turn. Re-check on a real recording (one short sample,
local only) before release 2.

## 3. Turn detection

Pipecat 1.15's default stop strategy is `TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3)`. It bundles
the smart-turn v3.2 CPU ONNX model, runs on onnxruntime and **doesn't need the torch-based `local-smart-turn`
extra**. Configuration used: `SileroVADAnalyzer(VADParams(stop_secs=0.2))` plus smart-turn
(`stop_secs` fallback 3 s).

- Inference ~230 ms on 1 CPU thread.
- 12 answers with 64 VAD pauses in total, including commas and sentence breaks inside answers: smart-turn judged
  every mid-answer pause as "incomplete" and ended each answer exactly once, **no premature cut-offs** (12/12 turns,
  every run).
- Silence-threshold alternative (`SpeechTimeoutUserTurnStopStrategy`, 0.8 s total) also ended 12/12 correctly.
  It isn't faster: speech end → turn end median 2.33 s versus 2.25 s with smart-turn. Turn end waits for the Whisper
  transcript either way (`wait_for_transcript=True`). With smart-turn, a long pause to think is judged on content,
  not on silence alone, which is the design's "err towards waiting".

**Choice: Silero VAD + local smart-turn v3.** Keep `SpeechTimeoutUserTurnStopStrategy` as a config fallback.

## 4. TTS

One-sentence interviewer questions (4 sentences × 2 runs; `spike/bench_tts*.py`, `spike/bench_piper.py`):

| engine | time to first audio, median | max | real-time factor | notes |
|---|---|---|---|---|
| Kokoro v1.0 fp32, onnxruntime default threads | 2.93 s | 3.57 s | 0.95 | |
| Kokoro v1.0 int8, default threads | 2.45 s | 3.85 s | 0.81 | |
| Kokoro int8, 4 intra-op threads | 2.7 s best | | 0.71 | 2–6 threads similar; 1 or 12 threads worse |
| **Piper en_US-lessac-medium** | **0.19 s** | 0.26 s | 0.08 | `pipecat-ai[piper]`, 63 MB voice |
| Piper en_GB-alba-medium | 0.24 s | 0.32 s | 0.10 | |

Kokoro streams one sentence at a time, so its time to first audio is roughly 0.5× the first sentence's spoken
length. Inside the pipeline, Kokoro took 1.0–1.5 s for short first sentences ("Thanks, Sam.") and 2.5–6 s for long
ones.

**Pipecat pitfall found:** `TTSService` closes an audio context after `stop_frame_timeout_s=3.0` s without audio.
Kokoro on this CPU can exceed that on a long first sentence. Pipecat then logs "completed with no audio", pushes an
ErrorFrame, and the reply's audio is mishandled. The spike sets `stop_frame_timeout_s=10`, and the real build must
too.

## 5. LLM: Claude streaming (`spike/bench_llm.py`, 6 calls each, ~420 input / ~30 output tokens)

| model (thinking setting) | first token, median | max | first full sentence, median |
|---|---|---|---|
| claude-haiku-5-5 (`thinking: disabled`) | 0.81 s | 1.06 s | 1.07 s |
| claude-sonnet-5-5 (`thinking: between_tools`) | 0.85 s | 0.95 s | 1.51 s |
| claude-sonnet-5-5 (API default, adaptive thinking) | 2.21 s (1 call) | | used all 150 output tokens on thinking; the next call returned no text |

Pipecat 1.15 sends each model's lowest thinking setting by default (`between_tools` for Sonnet 5.5, `disabled` for
Haiku 5.5). Don't override it for the interviewer. Inside the pipeline, Sonnet's time to first token was more
variable (0.9–4.5 s) and its first sentences were longer, which also costs TTS time.

Prompt caching: the spike's system prompt (~300 tokens) is below the minimum cacheable size, so no cache hits were
measured. A real brief (role + candidate summary + 8 questions with `probe_for`) will be near that minimum. Check
`cache_read_input_tokens` in the real build.

API spend for the whole spike: ≈ $0.08 (estimate from token counts: 2 Sonnet sessions + benchmark calls; Haiku
sessions are fractions of a cent).

## 6. End-to-end reply latency

Setup: Playwright drives `spike/client.html` (Pipecat JS client). The fake mic plays a 285 s WAV: 25 s of silence
for the greeting, then 12 answers, each followed by 14 s of silence. The container runs the full pipeline:
transport → Whisper → user aggregator (VAD + turn) → Claude → TTS → transport → recorder.

"Reply gap" = end of candidate speech → first agent audio, measured three ways:

- **browser**: energy on the fake-mic track versus the remote bot track (10 ms polling). This is the headline
  number.
- **recording**: the same rule on the stereo recording. For Kokoro + Haiku it gave a 5.15 s median, matching the
  browser's 5.11 s.
- **server**: `VADUserStoppedSpeakingFrame.timestamp − stop_secs` → `BotStartedSpeakingFrame`. This is ~0.4 s
  optimistic, because Silero ends speech before the audible tail does.

All rows: base.en, beam 1, 4 threads, smart-turn, SmallWebRTC unless stated, 12 turns each (≥10 required).

| config | browser median | browser p90 | server median / p90 | speech end → turn end | turn end → first LLM text | first LLM text → first TTS audio |
|---|---|---|---|---|---|---|
| Kokoro int8 + Haiku | 5.11 s | 6.71 s | 4.65 / 6.29 s | 2.25 s | 0.88 s | 1.51 s |
| Kokoro int8 + Haiku, **WebSocket** | n/a | n/a | 4.60 / 5.41 s | 2.06 s | 0.83 s | 1.85 s |
| Kokoro int8 + Haiku, silence 0.8 s | 7.82 s | 9.40 s | 6.41 / 7.89 s | 2.33 s | 0.89 s | 2.66 s (longer first sentences that run) |
| Kokoro int8 + Sonnet | 7.86 s | 8.95 s | 7.25 / 8.50 s (9 clean turns) | 2.34 s | 1.33 s | 3.34 s |
| **Piper + Haiku** | **3.87 s** | **4.02 s** | 3.47 / 3.67 s | 2.28 s | 0.84 s | 0.38 s |
| Piper + Sonnet | 5.90 s | 7.64 s | 5.41 / 7.03 s | 2.46 s | 1.29 s | 1.02 s |

Where the time goes, for Piper + Haiku: endpointing and STT ≈ 2.3 s (VAD 0.2 s + smart-turn 0.23 s running in
parallel with Whisper ~1.4 s, plus about 0.5 s of audio tail and pipeline overhead), LLM ≈ 0.85 s, first sentence
aggregation and TTS ≈ 0.4 s, WebRTC playout ≈ 0.1–0.4 s. **The largest remaining cost is Whisper running after the
turn ends.**

With Kokoro + Sonnet, long replies plus slow TTS ran into the next scripted answer 3 times (barge-in). Only 9 of 12
turns were clean.

Start to first agent audio (acceptance #1 budget, models preloaded): 2.0–3.9 s after the peer connection is up,
plus 2–3 s for `connect()` (7.3 s once, when the Piper voice was downloaded on first use). That is well within the 10 s budget.

## 7. Recording

- `AudioBufferProcessor(sample_rate=16000, num_channels=2)`, placed after `transport.output()`.
  `on_audio_data` gives interleaved stereo (user left, bot right), time-aligned with silence padding.
  `on_track_audio_data` gives the two mono tracks. Call `start_recording()` on client connect; `stop_recording()`
  on disconnect flushes everything (`buffer_size=0`).
- Measured: 5-minute session → 18.7 MB stereo 16-bit WAV.
- Mono Ogg/Opus with `soundfile.write(..., format="OGG", subtype="OPUS")` (libsndfile ships in the `soundfile`
  wheel, already a Pipecat dependency): **679 KB** for the same 5 minutes. Encoding took 21 s here while a session
  was running alongside. Do it once at session end, off the event loop.
- Recommended result format: `ogg_opus`, mono 16 kHz (both sides mixed), as §5.2 prefers. Keep the stereo WAV only
  as an optional debug output.

## 8. CPU and memory (container, during a live session; docker stats every ~2 s, 100% = 1 core)

| config | container CPU avg | peak | RSS (process) | Docker VM memory used, peak |
|---|---|---|---|---|
| Kokoro + Haiku | 2.6 cores | 6.4 cores | 1.2 GiB | 3.1 GiB (JobLens dev + prod included) |
| Kokoro + Haiku, silence | 2.9 cores | 8.9 cores | 1.1 GiB | 3.0 GiB |
| Kokoro + Sonnet | 2.9 cores | 9.1 cores | 1.1 GiB | 3.4 GiB |
| Piper + Haiku | 1.5 cores | 6.9 cores | 1.0 GiB | 3.0 GiB |
| Piper + Sonnet | 1.3 cores | 6.4 cores | 1.0 GiB | 2.9 GiB |

The models volume `iassist_models` holds 1.5 GB (Whisper base/small/distil-small, Kokoro fp32 + int8, Piper ×2).
The real image needs only base.en (~140 MB) + smart-turn (bundled) + Silero (bundled) + one TTS voice.

## Recommendation

1. **Transport: SmallWebRTC**, with the aioice port-range/host patch, a published UDP range of 2–3 ports per stack
   bound to 127.0.0.1, and signalling proxied by JobLens. Keep the WebSocket transport as a config fallback. It
   works today with no patch.
2. **STT: faster-whisper `base.en`, int8, beam 1, `cpu_threads=4`** (not "all"). Re-check accuracy on a short
   real-voice sample; `small.en` is the fallback at ~+2 s for each turn.
3. **Turn detection: Silero VAD (`stop_secs=0.2`) + local smart-turn v3 (ONNX).** No torch.
4. **TTS: this is the decision the architect needs to make.** With Kokoro, nothing gets under ~5 s median. With
   Piper, the median is 3.9 s (Haiku). Options:
   (a) Piper as the local default, Kokoro as a config option for nicer voice quality.
   (b) Kokoro, accepting ~5 s.
   (c) A paid streaming TTS later (design G4).
   The builder's suggestion is (a).
5. **Interviewer LLM default: `claude-haiku-5-5` for latency** (Piper + Haiku 3.9 s versus Piper + Sonnet 5.9 s
   median). If follow-up quality from Haiku proves weak in a live dev session, switch to Sonnet 5.5
   (`between_tools`), accepting ~2 s more. The design defaults to Sonnet for follow-up quality, so the architect
   should confirm or adjust acceptance #3: **3.5 s isn't met by any measured config; 3.9 s / 4.0 s (median / p90)
   is the best.**
6. Getting under 3.5 s would take one of these:
   - Whisper at 2 threads, or `tiny.en`, ~-0.5 s;
   - prompting the interviewer to open with a short clause (cuts first-sentence TTS and LLM time);
   - a paid streaming STT, which removes the ~1.4 s Whisper step after the turn ends.
   The Windows power mode ("Best performance" instead of Balanced) may also help, but it's a system setting and
   the spike didn't change it.

## Risks for the real build

- **aioice monkeypatch:** private API (`get_component_candidates`, `_protocols`). Pin aiortc/aioice and add a
  canary test.
- **Pipecat TTS 3 s audio-context timeout** (`stop_frame_timeout_s`): must be raised for local TTS.
- **Thread oversubscription:** Whisper, Kokoro and onnxruntime all slow down with all 12 threads on this hybrid CPU.
  Make thread counts settings (STT 4, TTS 4, smart-turn 1).
- **Variance:** this laptop's timings vary ±30% run to run (15 W U-series, Balanced power plan, WSL2). Judge p90,
  not only the median.
- **Echo/barge-in:** the fake mic has no echo. With speakers, the interviewer's own voice may trigger VAD and
  interrupt it. Headphones guidance stays; SmallWebRTC keeps browser AEC.
- **Long first sentences** (especially from Sonnet) directly add TTS time and caused 3 talk-overs with Kokoro. The
  prompt should ask for a short opening clause.
- **Tool calls before speaking:** the design's `ask_question` / `follow_up` tool calls must not add an LLM round
  trip before audio. Have the model speak and call the tool in the same response (text first), or track
  `question_id` without a blocking tool result. This wasn't measured here.
- **Logging:** Pipecat's DEBUG logs print transcript text and LLM context. The real service must run at INFO and
  filter, to keep the "no transcript text in logs" rule.
- **STT accuracy:** measured on synthetic speech only. A real voice, accent or room noise will be worse; base.en may
  need `small.en` (+2 s).
- **Prompt caching:** a small brief may fall under the minimum cacheable prefix, so no saving. Check the real
  brief's size.

## How to rerun

From Git Bash in `spike/`:

1. Build the image: `docker build -t iassist-spike:dev .`
2. Generate the candidate audio: `python gen_audio.py 14 25`, run in the container with `-v iassist_models:/models`
   and `-v <spike>:/app`.
3. Start the bot: `STT_MODEL=base.en TURN=smart LLM_MODEL=claude-haiku-5-5 TTS_PROVIDER=piper TAG=x ./run_bot.sh`.
   This reads only `ANTHROPIC_API_KEY` from the JobLens `.env`, into the process environment, and never prints it.
4. Drive a session: `node drive.mjs webrtc 292 <abs path to out/session_48k.wav> chromium|chrome|msedge`.
5. Summarise: `python summarize.py out`.
6. Benchmarks: `bench_stt.py`, `bench_threads.py`, `bench_tts.py`, `bench_tts_threads.py`, `bench_piper.py`,
   `bench_llm.py`, `rec_check.py`.

Outputs go to `spike/out/`, which git ignores. Stop the bot afterwards with `docker rm -f iassist-spike-bot`.
