"""Piper (pipecat-ai[piper]) on CPU as a faster local TTS alternative: time to first audio."""
import json, statistics, time
from pathlib import Path
from piper import PiperVoice
from piper.download_voices import download_voice
from common import MODELS, OUT
SENTENCES = [
    "Thanks, Sam. Tell me about a project you're proud of, and what your part in it was.",
    "How did you measure that improvement?",
    "What would you do differently if you ran that project again today?",
    "Why do you want to join Northwind Freight in particular?",
]
d = MODELS / "piper"; d.mkdir(parents=True, exist_ok=True)
res = []
for voice in ("en_US-lessac-medium", "en_GB-alba-medium"):
    if not (d / f"{voice}.onnx").exists():
        download_voice(voice, d)
    v = PiperVoice.load(d / f"{voice}.onnx")
    list(v.synthesize("Warm up."))
    firsts, tot, aud = [], 0.0, 0.0
    for s in SENTENCES * 2:
        t = time.perf_counter(); first = None
        for chunk in v.synthesize(s):
            first = first or time.perf_counter() - t
            aud += len(chunk.audio_int16_bytes) / 2 / chunk.sample_rate
        firsts.append(first); tot += time.perf_counter() - t
    r = {"voice": voice, "ttfa_median_s": round(statistics.median(firsts), 2), "ttfa_max_s": round(max(firsts), 2), "rtf": round(tot / aud, 3)}
    print(json.dumps(r), flush=True); res.append(r)
(OUT / "bench_piper.json").write_text(json.dumps(res, indent=1))
