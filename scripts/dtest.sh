#!/usr/bin/env bash
# Run a command in the dev test container (Git Bash at the repo root), e.g. ./scripts/dtest.sh pytest -q
set -euo pipefail
cd "$(dirname "$0")/.."
MSYS_NO_PATHCONV=1 docker compose -f docker/compose.yml --profile test run --rm tests "$@"
