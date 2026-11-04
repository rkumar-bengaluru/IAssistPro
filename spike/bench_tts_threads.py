"""Kokoro TTFA vs onnxruntime intra-op thread count."""
import asyncio, os, time, json
import onnxruntime as rt
from kokoro_onnx import Kokoro
from common import INTERVIEWER_VOICE, ensure_kokoro
S = "Thanks, Sam. Tell me about a project you're proud of, and what your part in it was."
async def ttfa(k):
    t = time.perf_counter(); first = None; audio = 0
    async for s, sr in k.create_stream(S, voice=INTERVIEWER_VOICE, lang="en-us"):
        first = first or time.perf_counter() - t; audio += len(s) / sr
    return first, time.perf_counter() - t, audio
async def main():
    for int8 in (False, True):
        m, v = ensure_kokoro(int8=int8)
        for th in [int(x) for x in os.environ.get("THREADS", "1,2,4,6,12").split(",")]:
            so = rt.SessionOptions(); so.intra_op_num_threads = th; so.inter_op_num_threads = 1
            k = Kokoro.from_session(rt.InferenceSession(str(m), so, providers=["CPUExecutionProvider"]), str(v))
            await ttfa(k)
            r = [await ttfa(k) for _ in range(3)]
            print(json.dumps({"model": m.name, "threads": th, "ttfa_best_s": round(min(x[0] for x in r), 2),
                              "ttfa_all": [round(x[0], 2) for x in r], "rtf": round(sum(x[1] for x in r) / sum(x[2] for x in r), 2)}), flush=True)
asyncio.run(main())
