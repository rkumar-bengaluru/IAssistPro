"""faster-whisper on CPU int8: accuracy (WER) and time per utterance for candidate models."""

import json
import os
import statistics
import time

import numpy as np
import psutil
import soundfile as sf
from faster_whisper import WhisperModel

from common import ANSWERS, OUT, wer

MODELS = os.environ.get("STT_MODELS", "base.en,small.en,distil-small.en").split(",")
THREADS = int(os.environ.get("STT_THREADS", os.cpu_count()))
rng = np.random.default_rng(0)
clips = []
for i, ref in enumerate(ANSWERS):
    a, sr = sf.read(OUT / "answers" / f"a{i:02d}_16k.wav", dtype="float32")
    noise = rng.normal(0, 1, len(a)).astype(np.float32)
    noise *= np.sqrt(np.mean(a**2) / 10 ** (20 / 10)) / np.sqrt(np.mean(noise**2))  # 20 dB SNR
    clips.append((ref, a, (a + noise).astype(np.float32)))

results = []
proc = psutil.Process()
for name in MODELS:
    t0 = time.perf_counter()
    m = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=THREADS, download_root=None)
    load_s = time.perf_counter() - t0
    m.transcribe(clips[0][1], language="en", beam_size=1)  # warm-up
    for beam in (1, 5):
        for variant in ("clean", "noisy"):
            times, wers, audio_s = [], [], 0.0
            for ref, clean, noisy in clips:
                a = clean if variant == "clean" else noisy
                t = time.perf_counter()
                segs, _ = m.transcribe(a, language="en", beam_size=beam)
                hyp = " ".join(s.text for s in segs).strip()
                times.append(time.perf_counter() - t)
                wers.append(wer(ref, hyp))
                audio_s += len(a) / 16000
            r = {
                "model": name, "threads": THREADS, "beam": beam, "audio": variant, "load_s": round(load_s, 1),
                "median_s": round(statistics.median(times), 2), "max_s": round(max(times), 2),
                "rtf": round(sum(times) / audio_s, 3), "wer_pct": round(100 * statistics.mean(wers), 1),
                "rss_mb": round(proc.memory_info().rss / 2**20),
            }
            results.append(r)
            print(json.dumps(r), flush=True)
            if beam == 1 and variant == "noisy":
                print("  sample hyp:", hyp, flush=True)
    del m
(OUT / "bench_stt.json").write_text(json.dumps(results, indent=1))
