"""Summarise live acceptance runs (e2e/out/live-*.json) against AT3: browser median <= 4500 ms and p90 <= 5500 ms
over at least 10 reply gaps, with no split answer. Prints counts and timings only, never transcript text.

usage: latency_report.py e2e/out/live-*.json
"""

import json
import statistics
import sys

MEDIAN_MAX_MS = 4500
P90_MAX_MS = 5500
MIN_GAPS = 10


def p90(xs: list[float]) -> float:
    s = sorted(xs)
    return s[max(0, round(0.9 * len(s)) - 1)]  # the spike rule, as transcript.p90


def report(path: str) -> bool:
    run = json.loads(open(path, encoding="utf-8").read())
    gaps = run.get("gaps") or []
    server = run.get("result_latency") or {}
    cache_read = sum(u.get("cached_input_tokens", 0) for u in run.get("usage", []) if u.get("kind", "llm") == "llm")
    med = statistics.median(gaps) if gaps else None
    p = p90(gaps) if gaps else None
    splits = run.get("splits")
    ok = bool(gaps) and len(gaps) >= MIN_GAPS and med <= MEDIAN_MAX_MS and p <= P90_MAX_MS and not splits
    print(f"{path}: n={len(gaps)} browser median={med} p90={p} server median={server.get('median_reply_ms')} "
          f"p90={server.get('p90_reply_ms')} n_server={server.get('n')} first_bot_audio_ms={run.get('first_bot_audio_ms')} "
          f"end={run.get('end_reason')} splits={splits} talk_over_ms={run.get('talk_over_ms')} "
          f"llm_runs={run.get('llm_runs')} resumes={server.get('resumes')} cache_read_input_tokens={cache_read} cost=${run.get('cost', 0):.4f} "
          f"{'PASS' if ok else 'FAIL'}")
    return ok


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    results = [report(p) for p in paths]
    return 0 if any(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
