#!/usr/bin/env bash
# Start the spike bot container (Git Bash on Windows). Only ANTHROPIC_API_KEY is read from the
# JobLens .env, into this process's environment; it is passed by name and never printed.
# usage: STT_MODEL=base.en TURN=smart LLM_MODEL=claude-haiku-5-5 TAG=x ./run_bot.sh
set -euo pipefail
export MSYS_NO_PATHCONV=1
ENV_FILE="${ENV_FILE:-C:/2026/rkumar/JobLens/.env}"
ANTHROPIC_API_KEY="$(grep -m1 '^ANTHROPIC_API_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r"'"'"'')"
export ANTHROPIC_API_KEY
docker rm -f iassist-spike-bot >/dev/null 2>&1 || true
docker run -d --name iassist-spike-bot \
  --label com.docker.compose.project=iassist-spike \
  -e ANTHROPIC_API_KEY \
  -e STT_MODEL="${STT_MODEL:-base.en}" -e STT_BEAM="${STT_BEAM:-1}" -e STT_THREADS="${STT_THREADS:-4}" \
  -e TURN="${TURN:-smart}" -e SILENCE_S="${SILENCE_S:-0.8}" -e LLM_MODEL="${LLM_MODEL:-claude-haiku-5-5}" \
  -e TTS_PROVIDER="${TTS_PROVIDER:-kokoro}" -e KOKORO_INT8="${KOKORO_INT8:-1}" -e KOKORO_THREADS="${KOKORO_THREADS:-4}" -e TAG="${TAG:-run}" \
  -e ICE_PORTS=40000-40009 -e ICE_HOST=127.0.0.1 \
  -p 127.0.0.1:8765:8765 -p 127.0.0.1:40000-40009:40000-40009/udp \
  -v iassist_models:/models -v "C:/2026/rkumar/IAssistPro/spike:/app" \
  iassist-spike:dev uvicorn bot:app --host 0.0.0.0 --port 8765
