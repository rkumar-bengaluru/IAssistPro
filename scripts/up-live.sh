#!/usr/bin/env bash
# Start the standalone interviewer with the real Anthropic LLM (Git Bash at the repo root).
# Only the ANTHROPIC_API_KEY= line of the JobLens .env is read, into this process's environment, and passed to
# compose by name. The value is never printed or written anywhere.
set -euo pipefail
set +x
cd "$(dirname "$0")/.."
ENV_FILE="${JOBLENS_ENV:-C:/2026/rkumar/JobLens/.env}"
ANTHROPIC_API_KEY="$(grep -m1 '^ANTHROPIC_API_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r"'"'"'')"
export ANTHROPIC_API_KEY
export MSYS_NO_PATHCONV=1
LLM_PROVIDER=anthropic docker compose -f docker/compose.yml up -d --build interviewer >/dev/null 2>&1 \n  || { echo "interviewer failed to start (re-run without the key: LLM_PROVIDER=fake docker compose -f docker/compose.yml up -d --build interviewer)"; exit 1; }
echo "interviewer starting (key passed by name)"
