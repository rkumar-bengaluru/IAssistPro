"""Recording check: stereo WAV from AudioBufferProcessor -> mono 16 kHz Ogg/Opus via soundfile (libsndfile),
plus reply gaps measured on the recording itself (user channel speech end -> bot channel audio start)."""
import sys, time
import numpy as np, soundfile as sf
src = sys.argv[1]
a, sr = sf.read(src, dtype="float32")
print("in:", src, a.shape, sr, f"{len(a)/sr:.0f}s")
mono = a.mean(axis=1)
t = time.perf_counter()
sf.write("/tmp/rec.ogg", mono, sr, format="OGG", subtype="OPUS")
import os
print(f"ogg/opus: {os.path.getsize('/tmp/rec.ogg')//1024} KB in {time.perf_counter()-t:.2f}s; wav stereo {os.path.getsize(src)//1024} KB")
# activity on each channel in 20 ms frames
fr = int(sr * 0.02)
def act(x):
    n = len(x) // fr
    e = np.sqrt((x[: n * fr].reshape(n, fr) ** 2).mean(axis=1))
    return e > 0.01
u, b = act(a[:, 0]), act(a[:, 1])
gaps, i = [], 0
starts = [k for k in range(1, len(b)) if b[k] and not b[k - 1:k].any() and not b[max(0, k - 25):k].any()]
prev = 0
for s in starts:
    ue = [k for k in range(prev, s) if u[k]]
    prev = s
    if ue and (s - ue[-1]) * 0.02 < 12:
        gaps.append(round((s - ue[-1]) * 0.02, 2))
print("recording gaps (s):", gaps, "median", float(np.median(gaps)) if gaps else None)
