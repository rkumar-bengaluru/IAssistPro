// Spoken e2e of the audio check (spec §3.1, acceptance 1–2): the real transport, STT and TTS with Chromium's fake
// microphone, and no LLM. Both tests are free; the service must run with LLM_PROVIDER=fake (the check never builds an
// LLM, but the guard keeps a paid stack from being used by accident).
// Build the speaking WAV first:
//   ./scripts/dtest.sh python scripts/make_session_wav.py --clips a12 --lead 7 --gap 10 --noise 0.002 --out e2e/out/check_ok.wav
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { ROOT, health, normalize, runCheck, writeSilentWav } from "./lib/harness";

const CHECK_OK = resolve(ROOT, "e2e/out/check_ok.wav");
const CHECK_SILENT = resolve(ROOT, "e2e/out/check_silent.wav");
const channel = process.env.E2E_CHANNEL ?? "chromium";

test("audio check passes with a speaking fake mic (acceptance 2)", async () => {
  test.skip((await health()).providers.llm.name !== "fake", "service not started with LLM_PROVIDER=fake");
  test.skip(!existsSync(CHECK_OK), "run scripts/make_session_wav.py for check_ok.wav first (see the file header)");
  const r = await runCheck({ wav: CHECK_OK, durS: 40, channel });
  const heard = r.server.heard.find((h: any) => h.text.trim() !== "");
  console.log(JSON.stringify({ heardAfterMs: heard ? heard.t - r.connectedAtMs : null, heard: heard?.text,
                               micLevels: r.server.mic_level, nonzero: r.server.mic_level_nonzero,
                               silent: r.server.silent_input, final: r.finalStatus }));
  expect(heard).toBeTruthy();
  expect(normalize(heard.text)).toEqual(expect.arrayContaining(["ready", "interview"]));
  expect(heard.t - r.connectedAtMs).toBeLessThan(30_000);
  expect(r.server.mic_level_nonzero).toBeGreaterThan(0);
  expect(r.server.silent_input).toEqual([]);           // quiet-room lead, never digital silence
  expect(r.finalStatus.state).toBe("ended");
});

test("audio check reports a silent input within 5 s (acceptance 1)", async () => {
  test.skip((await health()).providers.llm.name !== "fake", "service not started with LLM_PROVIDER=fake");
  writeSilentWav(CHECK_SILENT, 20);
  const r = await runCheck({ wav: CHECK_SILENT, durS: 12, channel });
  console.log(JSON.stringify({ silentAfterMs: r.server.silent_input.map((x: any) => x.t - r.connectedAtMs),
                               silent: r.server.silent_input, micLevels: r.server.mic_level,
                               nonzero: r.server.mic_level_nonzero, maxRms: r.server.mic_level_max,
                               heard: r.server.heard, final: r.finalStatus }));
  expect(r.server.silent_input.length).toBeGreaterThanOrEqual(1);
  expect(r.server.silent_input[0].t - r.connectedAtMs).toBeLessThan(5_000);
  // The codec's first 20 ms frame can carry a startup transient (seen: one window at 0.0001 before the warning);
  // what matters is that nothing after the warning looks like audio arriving, which would clear it in JobLens.
  expect(r.server.mic_level_nonzero).toBeLessThanOrEqual(1);
  expect(r.server.mic_level_nonzero_after_silent).toBe(0);
  expect(r.server.heard).toEqual([]);
});
