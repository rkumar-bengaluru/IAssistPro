# IAssistPro: voice mock interviewer

A standalone, generic voice mock-interviewer service. Any app that can send a **session brief** (a role, a candidate
summary and a list of questions) gets one live voice interview in the browser and, afterwards, a transcript with
question labels, per-model usage, reply timings and an Ogg/Opus recording. It knows nothing about the app that calls it.

- Transport: [Pipecat](https://github.com/pipecat-ai/pipecat) 1.15 SmallWebRTC (browser to container, peer to peer),
  with a WebSocket fallback.
- Speech: local on the CPU: faster-whisper `base.en` (int8, beam 1) and Piper `en_US-lessac-medium`; Kokoro is an
  option. Turn detection: Silero VAD + smart-turn v3.
- Interviewer: Claude (default `claude-haiku-5-5`) with tool calls that mark each main question and follow-up.
- One session at a time. Results are temporary: deleted on `DELETE` or after 24 hours.

## Quick start (standalone)

```bash
docker compose -f docker/compose.yml up -d --build interviewer        # needs ANTHROPIC_API_KEY in your environment
# or, with a key in another .env, without printing it:  ./scripts/up-live.sh
# or, free and scripted (no LLM):  LLM_PROVIDER=fake docker compose -f docker/compose.yml up -d --build interviewer
```

Wait until `curl -s 127.0.0.1:8770/health` says `"status":"ready"`. The first start downloads ~210 MB of models once
into the `interviewer_models` volume (`progress` shows how far it is).

```bash
curl -s -X POST localhost:8770/sessions -H 'content-type: application/json' -d @contracts/examples/brief.example.json
```

Open `http://127.0.0.1:8770/dev/client?session=<session_id>&token=<connect.token>` within 2 minutes (the token
expires), **with headphones**, and talk. Then `POST /sessions/<id>/end`, `GET /sessions/<id>/result`,
`GET /sessions/<id>/audio` (about a minute after the end) and `DELETE /sessions/<id>`.

## Service interface (contract version 1)

The container listens on `PORT` (default `8000`). WebRTC media uses the UDP range in `ICE_PORTS`, which **must be
published 1:1** (host port = container port), because the advertised candidate is `ICE_HOST:<port>`. Ports per stack:

| Stack | HTTP | UDP (`ICE_PORTS`) |
|---|---|---|
| JobLens prod | compose network only (`interviewer:8000`) | `40000-40002` |
| JobLens dev | compose network only | `40010-40012` |
| Standalone (this repo's compose) | `127.0.0.1:8770` | `40020-40022` |

Volumes: `/models` (model weights) and `/data/sessions` (temporary results).

### Endpoints

Errors are `{"detail": {"code": "<code>", "message": "<text>", ...}}`.

| Method, path | Success | Errors |
|---|---|---|
| `GET /health` | 200 `Health` (always 200 while the process is up) | – |
| `POST /sessions` body `Brief` | 201 `{session_id, connect}` | 422 `invalid_brief`; 409 `busy` (no session id); 503 `not_ready` (+`status`) |
| `GET /sessions/{id}` | 200 `Status` | 404 `unknown_session` |
| `POST /sessions/{id}/offer` (SDP offer JSON; token in `X-Session-Token` or `?token=`) | 200 SDP answer `{sdp, type, pc_id}` | 401 `token_invalid`; 410 `token_expired`; 409 `wrong_state` |
| `PATCH /sessions/{id}/offer` (`{pc_id, candidates}`, same token) | 200 `{"status":"success"}` | as above |
| `WS /sessions/{id}/ws?token=` (`TRANSPORT=websocket` only; Pipecat protobuf serializer) | – | close 4401/4410/4409 |
| `POST /sessions/{id}/end` | 202 `Status` (idempotent) | 404 |
| `GET /sessions/{id}/result` | 200 `Result` (states `ended`/`failed`) | 409 `not_finished`; 404 |
| `GET /sessions/{id}/audio` | 200 `audio/ogg` | 409 `audio_pending`; 404 `no_audio`/`unknown_session` |
| `DELETE /sessions/{id}` | 204 (removes the temp folder) | 409 `wrong_state` while waiting/live; 404 |
| `GET /dev/client` | 200 HTML (only with `DEV_CLIENT=1`) | 404 |

`connect` is `{"transport": "smallwebrtc", "offer_path": "/sessions/{id}/offer", "token": "<one-time>", "expires_at":
"<ISO 8601 UTC>"}`, or for websocket `{"transport": "websocket", "ws_path": "/sessions/{id}/ws", "token", "expires_at",
"audio": {"in_sample_rate": 16000, "out_sample_rate": 24000}}`. A token works for one offer within
`CONNECT_TOKEN_TTL_S`; after that only for `PATCH` on that session's `pc_id` until it ends. Apps should send the token
server-side in `X-Session-Token`; `?token=` exists for the dev client.

`Health.status` is `loading_models` (with `progress: {done_mb, total_mb, current}`), `ready`, or `error` with
`error` one of `missing_key:<VAR>`, `model_download_failed:<asset>`, `config:<message>`. `Health.busy` is true while
a session is waiting or live. A down service is "connection refused".

A session id is 128 random bits (32 hex characters) and is the only key to that session's status, result and audio.
It is returned only to the caller that created the session: health and `409 busy` never carry it.

`Status` is `{session_id, state: waiting|live|ended|failed, elapsed_s, current_question_id, question_number,
questions_total, turn_kind, end_reason, error, audio_state: pending|ready|none|failed}`. Lifecycle: `waiting` →
(offer) `live` → `ended`; an unused session becomes `failed` (`not_connected`) after the token TTL, and a crash or
restart turns a live session into `failed` (`service_restarted`) with the turns and usage written so far. The result
is available as soon as the session ends; the audio follows about a minute later.

Live captions are Pipecat RTVI events on the WebRTC data channel (`onUserTranscript` with `final`, `onBotTranscript`).
Every candidate turn carries the interviewer's latest `question_id` and `kind`. Usage `input_tokens` is Anthropic's
uncached input (cache reads and writes are separate fields).

`Brief` and `Result`: JSON Schemas in `contracts/brief.v1.schema.json` and `contracts/result.v1.schema.json`, examples
in `contracts/examples/` (an invented candidate). A brief with an email address, a URL or a phone number anywhere is
refused with 422; year ranges such as 2019-2023 are fine.

### Environment

| Var | Default | Notes |
|---|---|---|
| `PORT` | `8000` | HTTP |
| `TRANSPORT` | `smallwebrtc` | or `websocket` (fallback) |
| `ICE_PORTS` | `40010-40012` | must equal the published UDP range (at most 10 ports) |
| `ICE_HOST` | `127.0.0.1` | advertised host candidate |
| `STT_PROVIDER` / `STT_MODEL` / `STT_BEAM` / `STT_THREADS` | `whisper` / `base.en` / `1` / `4` | `small.en` is the fallback model |
| `TTS_PROVIDER` / `TTS_VOICE` / `TTS_THREADS` | `piper` / provider default / `4` | `piper`, `kokoro` (`af_heart`), `cartesia` (paid; `TTS_VOICE` required) |
| `TURN_STOP` / `SILENCE_S` | `smart` / `0.8` | `silence` = fallback |
| `LLM_PROVIDER` / `LLM_MODEL` | `anthropic` / `claude-haiku-5-5` | `fake` = scripted; the brief's `llm_model` overrides `LLM_MODEL` |
| `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY` | – | read only when that provider is selected |
| `MODELS_DIR` / `SESSIONS_DIR` | `/models` / `/data/sessions` | |
| `RESULT_TTL_H` / `CONNECT_TOKEN_TTL_S` | `24` / `120` | |
| `DEV_CLIENT` | `0` (`1` in this repo's compose) | serves `/dev/client` |
| `DEBUG_STEREO_WAV` | `0` | `1` keeps a stereo WAV next to the Ogg |
| `LOG_LEVEL` | `INFO` | |

## Providers

`src/iassistpro/providers.py` maps each `*_PROVIDER` value to a Pipecat service through one `REGISTRY` entry
(`ProviderSpec(kind, name, remote, key_env, build)`). Adding a paid provider is one entry plus its key; nothing else
changes. Cartesia TTS is the worked example: `TTS_PROVIDER=cartesia`, `TTS_VOICE=<voice id>`, `CARTESIA_API_KEY`.
A missing key for a selected provider shows as health `error: "missing_key:<VAR>"`. Paid speech reports its units
(characters or seconds) as extra usage rows.

## Privacy

- Logs carry timings, sizes, counts, ids and error codes only: never transcript text, brief content, SDP or audio.
  Third-party libraries log at WARNING and above only; the access log is off (it would print `?token=`). A canary
  test proves it.
- Session folders hold the turns, usage and recording, never the brief text. They are deleted on `DELETE` or after
  `RESULT_TTL_H` hours.
- With local speech the voice never leaves the machine. A paid STT provider would send it to that provider.

## Tests

```bash
./scripts/dtest.sh pytest -q              # unit, pipeline and API tests in the dev container
./scripts/dtest.sh pytest -q -m slow      # also builds the real local speech services
cd e2e && npm ci && npx playwright test   # spoken end-to-end through the real transport (service must be up)
```

The spoken e2e feeds generated candidate answers (`scripts/make_session_wav.py`, into `e2e/out/`) to the browser's fake
microphone. The free test needs the service started with `LLM_PROVIDER=fake`. The paid test runs only with
`IASSIST_LIVE=1` against a service started by `./scripts/up-live.sh`, and fails if the session cost passes
`E2E_MAX_SPEND_USD` (default `0.10`).

## Known limits

- One session at a time (the CPU can't run two).
- Reply latency on a laptop CPU is about 4 s (end of speech to first interviewer audio); paid streaming speech is the
  route below that.
- No reconnect: a dropped call ends the session as `disconnected`.
- The WebSocket fallback has weaker echo cancellation than WebRTC: use headphones.

## Licence

MIT.
