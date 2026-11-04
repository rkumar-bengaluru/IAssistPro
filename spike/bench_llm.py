"""Claude streaming: time to first token and to first complete sentence (what Kokoro needs)."""

import json
import os
import re
import statistics
import sys
import time

import anthropic

from bot import SYSTEM
from common import ANSWERS, OUT

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
res = []
for model in ("claude-haiku-5-5", "claude-sonnet-5-5"):
    ttft, ttfs, usage = [], [], []
    for i in range(N):
        msgs = [{"role": "user", "content": "(The candidate has joined the call.)"},
                {"role": "assistant", "content": "Hi Sam, thanks for joining. This is for the Backend Engineer role at Northwind Freight. Tell me about yourself."},
                {"role": "user", "content": ANSWERS[i % 4]}]
        t = time.perf_counter()
        first = sent = None
        text = ""
        # Same as Pipecat's default for these models: the lowest thinking setting.
        thinking = {"type": "between_tools"} if "sonnet" in model else {"type": "disabled"}
        with client.messages.stream(model=model, max_tokens=150, thinking=thinking, system=[{"type": "text", "text": SYSTEM}],
                                    messages=msgs) as s:
            for chunk in s.text_stream:
                if first is None and chunk.strip():
                    first = time.perf_counter() - t
                text += chunk
                if sent is None and re.search(r"[.?!](\s|$)", text):
                    sent = time.perf_counter() - t
            u = s.get_final_message().usage
        first = first if first is not None else float("nan")
        ttft.append(first); ttfs.append(sent or (time.perf_counter() - t))
        usage.append({"in": u.input_tokens, "out": u.output_tokens})
        print(model, round(first, 2), round(ttfs[-1], 2), u.input_tokens, u.output_tokens, flush=True)
    r = {"model": model, "n": N, "ttft_median_s": round(statistics.median(ttft), 2), "ttft_max_s": round(max(ttft), 2),
         "first_sentence_median_s": round(statistics.median(ttfs), 2),
         "tokens_in": sum(x["in"] for x in usage), "tokens_out": sum(x["out"] for x in usage)}
    print(json.dumps(r), flush=True)
    res.append(r)
(OUT / "bench_llm.json").write_text(json.dumps(res, indent=1))
