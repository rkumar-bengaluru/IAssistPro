"""Voice the invented candidate's answers (tests/audio/candidate/answers.json) with Kokoro am_michael and write
16 kHz mono PCM16 clips aNN.wav next to it. Run in the dev container: ./scripts/dtest.sh python scripts/gen_candidate_clips.py
"""

import json
import os
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "tests" / "audio" / "candidate"
VOICE = "am_michael"


def main() -> None:
    from kokoro_onnx import Kokoro

    from iassistpro.assets import _fetch_kokoro
    from iassistpro.config import Settings

    s = Settings()
    _fetch_kokoro(s)
    kdir = Path(os.environ.get("MODELS_DIR", "/models")) / "kokoro"
    model = kdir / "kokoro-v1.0.onnx"
    if not model.exists():
        model = kdir / "kokoro-v1.0.int8.onnx"
    k = Kokoro(str(model), str(kdir / "voices-v1.0.bin"))
    for a in json.loads((CLIPS / "answers.json").read_text()):
        samples, sr = k.create(a["text"], voice=VOICE, speed=1.0, lang="en-us")
        clip = soxr.resample(np.asarray(samples, dtype=np.float32), sr, 16000)
        sf.write(CLIPS / f"{a['id']}.wav", clip, 16000, subtype="PCM_16")
        print(f"{a['id']} {len(clip) / 16000:.2f}s", flush=True)


if __name__ == "__main__":
    main()
