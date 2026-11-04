"""Summarise live acceptance runs (e2e/out/live-*.json) against AS3: browser median <= 4000 ms and p90 <= 5000 ms
over at least 10 reply gaps. Prints counts and timings only, never transcript text.

usage: latency_report.py e2e/out/live-*.json
"""

import json
import statistics
import sys

MEDIAN_MAX_MS = 4000
P90_MAX_MS = 5000
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
    ok = bool(gaps) and len(gaps) >= MIN_GAPS and med <= MEDIAN_MAX_MS and p <= P90_MAX_MS
    print(f"{path}: n={len(gaps)} browser median={med} p90={p} server median={server.get('median_reply_ms')} "
          f"p90={server.get('p90_reply_ms')} n_server={server.get('n')} first_bot_audio_ms={run.get('first_bot_audio_ms')} "
          f"end={run.get('end_reason')} cache_read_input_tokens={cache_read} cost=${run.get('cost', 0):.4f} "
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
