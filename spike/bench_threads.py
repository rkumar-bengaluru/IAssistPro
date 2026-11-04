"""Thread-count sweep for faster-whisper int8 (and a check of what CTranslate2 sees)."""
import os, sys, time, json
import ctranslate2, soundfile as sf
from faster_whisper import WhisperModel
from common import OUT
print("ct2", ctranslate2.__version__, "cpu_count", os.cpu_count(), "compute types", ctranslate2.get_supported_compute_types("cpu"), flush=True)
name = sys.argv[1] if len(sys.argv) > 1 else "base.en"
a, _ = sf.read(OUT / "answers" / "a00_16k.wav", dtype="float32")
for th in [int(x) for x in os.environ.get("THREADS", "2,4,6,8,12").split(",")]:
    m = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=th)
    list(m.transcribe(a, language="en", beam_size=1)[0])
    ts = []
    for _ in range(3):
        t = time.perf_counter(); list(m.transcribe(a, language="en", beam_size=1)[0]); ts.append(time.perf_counter() - t)
    print(json.dumps({"model": name, "threads": th, "best_s": round(min(ts), 2), "all": [round(x, 2) for x in ts]}), flush=True)
