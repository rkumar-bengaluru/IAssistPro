"""Build a fake-microphone session WAV for the e2e (48 kHz mono PCM16) from the committed candidate clips:
`lead` seconds of silence (the greeting), then each clip followed by `gap` seconds (the interviewer replies).
Writes <out> and <out stem>.schedule.json. Output goes to e2e/out/ (git-ignored).

usage: make_session_wav.py --clips a00,a01 --lead 25 --gap 14 --out e2e/out/session_fake.wav
       [--noise 0.002]  adds seeded uniform noise of that full-scale amplitude to the whole file, so the leads and gaps
                        are a quiet room rather than digital silence (which the service reports as silent_input)
"""

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "tests" / "audio" / "candidate"
SR = 48000
GAIN = 0.8


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--clips", required=True)
    p.add_argument("--lead", type=float, default=25.0)
    p.add_argument("--gap", type=float, default=14.0)
    p.add_argument("--out", required=True)
    p.add_argument("--noise", type=float, default=0.0)
    a = p.parse_args()
    texts = {x["id"]: x["text"] for x in json.loads((CLIPS / "answers.json").read_text())}
    parts = [np.zeros(int(SR * a.lead), dtype=np.float32)]
    schedule, t = [], a.lead
    for cid in a.clips.split(","):
        clip, sr = sf.read(CLIPS / f"{cid}.wav", dtype="float32")
        up = soxr.resample(clip, sr, SR) * GAIN
        dur = len(up) / SR
        schedule.append({"id": cid, "start_s": round(t, 3), "end_s": round(t + dur, 3), "text": texts[cid]})
        parts += [up, np.zeros(int(SR * a.gap), dtype=np.float32)]
        t += dur + a.gap
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    audio = np.concatenate(parts)
    if a.noise > 0:
        audio = audio + np.random.default_rng(7).uniform(-a.noise, a.noise, len(audio)).astype(np.float32)
    sf.write(out, audio, SR, subtype="PCM_16")
    out.with_name(out.stem + ".schedule.json").write_text(json.dumps(
        {"lead_s": a.lead, "gap_s": a.gap, "total_s": round(t, 3), "clips": schedule}, indent=1))
    print(f"{out} {t:.1f}s {len(schedule)} clips")


if __name__ == "__main__":
    main()
