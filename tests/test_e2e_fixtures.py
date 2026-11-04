import json
from pathlib import Path

import soundfile as sf

from iassistpro.contract import Brief

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "tests" / "audio" / "candidate"


def test_committed_clips_are_16k_mono_and_match_answers():
    answers = json.loads((CLIPS / "answers.json").read_text())
    assert [a["id"] for a in answers] == [f"a{i:02d}" for i in range(12)]
    wavs = sorted(CLIPS.glob("a*.wav"))
    assert [w.stem for w in wavs] == [a["id"] for a in answers]
    for w in wavs:
        info = sf.info(w)
        assert (info.samplerate, info.channels, info.subtype) == (16000, 1, "PCM_16") and info.duration > 1.0
    assert sum(w.stat().st_size for w in wavs) < 4 * 2**20


def test_e2e_briefs_validate():
    for name in ("brief.e2e.json", "brief.live.json"):
        b = Brief.model_validate(json.loads((ROOT / "e2e" / "fixtures" / name).read_text()))
        assert b.candidate.name == "Sam" and b.role.company == "Northwind Freight"
        assert b.limits.max_llm_cost_usd == 0.05
    e2e = Brief.model_validate(json.loads((ROOT / "e2e" / "fixtures" / "brief.e2e.json").read_text()))
    live = Brief.model_validate(json.loads((ROOT / "e2e" / "fixtures" / "brief.live.json").read_text()))
    assert (len(e2e.questions), e2e.limits.max_follow_ups, e2e.limits.max_minutes) == (5, 1, 8)
    assert (len(live.questions), live.limits.max_minutes, live.llm_model) == (8, 7, "claude-haiku-5-5")
