"""Kokoro on CPU: time to first audio for a one-sentence question (fp32 vs int8 model)."""

import asyncio
import json
import statistics
import time

from kokoro_onnx import Kokoro

from common import INTERVIEWER_VOICE, OUT, ensure_kokoro

SENTENCES = [
    "Thanks, Sam. Tell me about a project you're proud of, and what your part in it was.",
    "How did you measure that improvement?",
    "What would you do differently if you ran that project again today?",
    "Why do you want to join Northwind Freight in particular?",
]


async def ttfa(k, text):
    t = time.perf_counter()
    first = None
    total_audio = 0.0
    async for samples, sr in k.create_stream(text, voice=INTERVIEWER_VOICE, lang="en-us"):
        if first is None:
            first = time.perf_counter() - t
        total_audio += len(samples) / sr
    return first, time.perf_counter() - t, total_audio


async def main():
    res = []
    for int8 in (False, True):
        m, v = ensure_kokoro(int8=int8)
        t = time.perf_counter()
        k = Kokoro(str(m), str(v))
        load = time.perf_counter() - t
        await ttfa(k, "Warm up.")
        firsts, totals, audio = [], [], []
        for s in SENTENCES * 2:
            f, tot, a = await ttfa(k, s)
            firsts.append(f); totals.append(tot); audio.append(a)
        r = {"model": m.name, "load_s": round(load, 1), "ttfa_median_s": round(statistics.median(firsts), 2),
             "ttfa_max_s": round(max(firsts), 2), "rtf": round(sum(totals) / sum(audio), 3),
             "audio_s_median": round(statistics.median(audio), 1)}
        print(json.dumps(r), flush=True)
        res.append(r)
    (OUT / "bench_tts.json").write_text(json.dumps(res, indent=1))


asyncio.run(main())
