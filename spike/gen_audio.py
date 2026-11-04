"""Generate the invented candidate's spoken answers with Kokoro (24 kHz) plus 16 kHz copies,
and a long 'session' WAV (answers separated by silence) for Chrome's fake microphone."""

import json
import sys

import numpy as np
import soundfile as sf
import soxr
from kokoro_onnx import Kokoro

from common import ANSWERS, CANDIDATE_VOICE, OUT, ensure_kokoro

GAP_S = float(sys.argv[1]) if len(sys.argv) > 1 else 14.0  # silence after each answer (agent replies here)
LEAD_S = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0  # silence before first answer (greeting)

model, voices = ensure_kokoro()
k = Kokoro(str(model), str(voices))
d = OUT / "answers"
d.mkdir(parents=True, exist_ok=True)
session = [np.zeros(int(48000 * LEAD_S), dtype=np.float32)]
schedule, t = [], LEAD_S
for i, text in enumerate(ANSWERS):
    samples, sr = k.create(text, voice=CANDIDATE_VOICE, speed=1.0, lang="en-us")
    samples = samples.astype(np.float32)
    sf.write(d / f"a{i:02d}_24k.wav", samples, sr, subtype="PCM_16")
    sf.write(d / f"a{i:02d}_16k.wav", soxr.resample(samples, sr, 16000), 16000, subtype="PCM_16")
    s48 = soxr.resample(samples, sr, 48000) * 0.8
    dur = len(s48) / 48000
    schedule.append({"i": i, "start_s": round(t, 3), "end_s": round(t + dur, 3), "text": text})
    session += [s48, np.zeros(int(48000 * GAP_S), dtype=np.float32)]
    t += dur + GAP_S
    print(f"a{i:02d} {dur:.2f}s", flush=True)
sf.write(OUT / "session_48k.wav", np.concatenate(session), 48000, subtype="PCM_16")
(OUT / "session_schedule.json").write_text(json.dumps(schedule, indent=1))
print(f"session {t:.1f}s")
