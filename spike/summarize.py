"""Summarise spike session results (out/*.json) into the tables used in the report."""
import glob, json, statistics, sys, wave
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "out")


def p90(xs):
    xs = sorted(xs)
    return xs[max(0, round(0.9 * len(xs)) - 1)]


for f in sorted(OUT.glob("*_*.json")):
    if "browser" in f.name or f.name.startswith("bench"):
        continue
    d = json.loads(f.read_text())
    if not isinstance(d, dict) or "turns" not in d:
        continue
    turns = [t for t in d["turns"] if t.get("reply_ms") and t.get("speech_end_est")]
    if not turns:
        continue
    def part(a, b):
        v = [t[b] - (t[a] if a != "se" else t["speech_end_est"]) for t in turns if b in t and (a == "se" or a in t)]
        return round(1000 * statistics.median(v)) if v else None
    gaps = [t["reply_ms"] for t in turns]
    br = sorted(OUT.glob(f"{d['tag']}_browser_*.json"))
    bg = json.loads(br[-1].read_text())["gaps"] if br else []
    rec = OUT / f"{f.stem}.stereo.wav"
    recinfo = ""
    if rec.exists():
        with wave.open(str(rec)) as w:
            recinfo = f"{w.getnchannels()}ch {w.getframerate()}Hz {w.getnframes() / w.getframerate():.0f}s {rec.stat().st_size // 1024}KB"
    print(json.dumps({
        "tag": d["tag"], "n": len(gaps), "server_median_ms": statistics.median(gaps), "server_p90_ms": p90(gaps),
        "browser_n": len(bg), "browser_median_ms": statistics.median(bg) if bg else None, "browser_p90_ms": p90(bg) if bg else None,
        "speech_end_to_turn_end_ms": part("se", "turn_end"), "turn_end_to_llm_text_ms": part("turn_end", "llm_first_text"),
        "llm_text_to_tts_audio_ms": part("llm_first_text", "tts_first_audio"),
        "ttfb_llm_ms": round(1000 * statistics.median(t["ttfb"]["AnthropicLLMService"] for t in turns if "AnthropicLLMService" in t.get("ttfb", {}))),
        "ttfb_tts_ms": round(1000 * statistics.median(v for t in turns for k, v in t.get("ttfb", {}).items() if "TTS" in k)),
        "connect_to_first_bot_audio_s": d["connect_to_first_bot_audio_s"], "resources": d["resources"], "recording": recinfo,
    }))
