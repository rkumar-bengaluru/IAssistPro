# Throwaway spike image: Pipecat + local speech on CPU.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
ARG PIPECAT_VERSION=1.15.0
RUN uv pip install --system --no-cache \
    "pipecat-ai[anthropic,kokoro,piper,silero,whisper,webrtc,websocket,runner]==${PIPECAT_VERSION}" \
    psutil
ENV HF_HOME=/models/hf \
    MODELS_DIR=/models \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
