# IAssistPro interviewer service. Stages: base (runtime deps), dev (tests, source mounted), runtime.
FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 espeak-ng \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH=/opt/venv/bin:$PATH \
    HF_HOME=/models/hf \
    MODELS_DIR=/models \
    SESSIONS_DIR=/data/sessions \
    PYTHONUNBUFFERED=1

FROM base AS dev
RUN uv sync --frozen --no-install-project
ENV PYTHONPATH=/app/src
