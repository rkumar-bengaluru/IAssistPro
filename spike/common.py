"""Shared spike data: an invented candidate ("Sam Rivera", fictional) and helpers."""

import os
import re
from pathlib import Path

MODELS = Path(os.environ.get("MODELS_DIR", "/models"))
OUT = Path(os.environ.get("SPIKE_OUT", "/app/out"))
KOKORO_DIR = MODELS / "kokoro"
KOKORO_URLS = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
CANDIDATE_VOICE = "am_michael"
INTERVIEWER_VOICE = "af_heart"

# Invented candidate answers (no real person). Mixed lengths, some numbers and tool names.
ANSWERS = [
    "Hi, I'm Sam. I've spent six years building backend services, mostly in Python and Go, for a logistics company.",
    "The project I'm proudest of is a routing service that cut delivery planning time from two hours to about ten minutes.",
    "I led a team of four engineers. My part was the data model and the queue design; the others owned the user interface and testing.",
    "We measured it by comparing planning logs for three months before and after the release.",
    "When two senior people disagreed on the database choice, I set up a small benchmark so we could decide on evidence instead of opinion.",
    "I'd say my weakest area is frontend work. I can read React code, but I haven't shipped a large feature on my own.",
    "I want this role because the team works on real time systems at scale, and that's exactly where I want to grow next.",
    "In my first ninety days I would learn the deployment pipeline, fix a few small bugs, and then take ownership of one service.",
    "Kubernetes, Postgres and Kafka are the tools I use every day, and I've written Terraform for most of our staging setup.",
    "One mistake I made was skipping load tests before a launch. We had an outage for forty minutes, and since then I always test at twice the expected traffic.",
    "Sure. Outside work I mentor two junior developers and I run a small study group on distributed systems.",
    "No, I think that covers it. Thank you for your time today.",
]


def ensure_kokoro(int8: bool = False) -> tuple[Path, Path]:
    import requests

    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    names = ["kokoro-v1.0.int8.onnx" if int8 else "kokoro-v1.0.onnx", "voices-v1.0.bin"]
    for n in names:
        p = KOKORO_DIR / n
        if not p.exists():
            print(f"downloading {n} ...", flush=True)
            with requests.get(KOKORO_URLS[n], stream=True, timeout=600) as r:
                r.raise_for_status()
                tmp = p.with_suffix(".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                tmp.rename(p)
    return KOKORO_DIR / names[0], KOKORO_DIR / names[1]


_NUM = {"six": "6", "two": "2", "ten": "10", "four": "4", "three": "3", "ninety": "90", "forty": "40", "twice": "twice"}


def normalize(text: str) -> list[str]:
    t = text.lower().replace("-", " ")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    words = [_NUM.get(w, w) for w in t.split()]
    return words


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref), normalize(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / max(1, len(r))
