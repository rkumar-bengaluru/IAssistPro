// Spoken end-to-end session through the real transport, STT and TTS, with a fake microphone.
// Free test: service started with LLM_PROVIDER=fake. Paid test: IASSIST_LIVE=1 and ./scripts/up-live.sh.
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import {
  MAX_SPEND, ROOT, candidateText, costUsd, health, median, p90, readJson, runSession, splitCount, validateResult, wer,
} from "./lib/harness";

const e2eBrief = readJson("e2e/fixtures/brief.e2e.json");
const liveBrief = readJson("e2e/fixtures/brief.live.json");
const SESSION_FAKE = resolve(ROOT, "e2e/out/session_fake.wav");
const SESSION_LIVE = resolve(ROOT, "e2e/out/session_live.wav");
const fakeSchedule = () => readJson("e2e/out/session_fake.schedule.json");
const liveSchedule = () => readJson("e2e/out/session_live.schedule.json");

test("spoken session with the scripted interviewer (free)", async () => {
  test.skip(!existsSync(SESSION_FAKE), "run scripts/make_session_wav.py first (see README)");
  test.skip((await health()).providers.llm.name !== "fake", "service not started with LLM_PROVIDER=fake");
  const sched = fakeSchedule();
  const r = await runSession({ brief: e2eBrief, wav: SESSION_FAKE, durS: Math.ceil(sched.total_s) + 40,
                               channel: process.env.E2E_CHANNEL ?? "chromium" });
  const scriptedText = sched.clips.map((c: any) => c.text).join(" ");
  console.log(JSON.stringify({ end: r.result.end_reason, gaps: r.gaps, latency: r.result.latency, firstBotAudioMs: r.firstBotAudioMs,
                               createMs: r.createMs, captions: r.captions, wer: wer(scriptedText, candidateText(r.result)),
                               splits: splitCount(r.result, sched.clips), talkOverMs: r.talkOverMs, llmRuns: r.llmRuns }));
  expect(r.result.end_reason).toBe("completed");
  expect(r.result.turns.filter((t: any) => t.speaker === "candidate").map((t: any) => [t.question_id, t.kind]))
    .toEqual([["q1", "main"], ["q2", "main"], ["q2", "follow_up"], ["q3", "main"], ["q4", "main"], ["q5", "main"]]);
  expect(wer(scriptedText, candidateText(r.result))).toBeLessThan(0.15);
  expect(r.result.audio.state).toBe("ready");
  expect(r.result.latency.n).toBeGreaterThanOrEqual(5);
  expect(r.gaps.length).toBeGreaterThanOrEqual(5);
  expect(r.captions.user).toBeGreaterThan(0);
  expect(r.captions.bot).toBeGreaterThan(0);
  expect(r.createMs + (r.firstBotAudioMs ?? 1e9)).toBeLessThan(10_000);   // acceptance #1 (models warm)
  expect(validateResult(r.result)).toBe(true);
  // acceptance #2 (AT3): no split answer, no talk-over, and no extra LLM run (greeting + one per answer)
  expect(splitCount(r.result, sched.clips)).toBe(0);
  expect(r.talkOverMs).toBe(0);
  expect(r.llmRuns).toBe(sched.clips.length + 1);
});

test("live session with Claude Haiku (paid, opt-in)", async () => {
  test.skip(process.env.IASSIST_LIVE !== "1", "set IASSIST_LIVE=1 to spend (cap $" + MAX_SPEND + ")");
  test.skip((await health()).providers.llm.name !== "anthropic", "service not started with scripts/up-live.sh");
  test.skip(!existsSync(SESSION_LIVE), "run scripts/make_session_wav.py first (see README)");
  const r = await runSession({ brief: liveBrief, wav: SESSION_LIVE, durS: 300,
                               channel: process.env.E2E_CHANNEL ?? "chromium", endAfterS: 300 });
  const cost = costUsd(r.result, liveBrief.llm_prices_per_mtok);
  console.log(`live spend $${cost.toFixed(4)}`);
  const splits = splitCount(r.result, liveSchedule().clips.slice(0, r.result.turns.filter((t: any) => t.speaker === "candidate").length));
  console.log(JSON.stringify({ end: r.result.end_reason, splits, talkOverMs: r.talkOverMs, llmRuns: r.llmRuns, n: r.gaps.length, median: r.gaps.length ? median(r.gaps) : null,
                               p90: r.gaps.length ? p90(r.gaps) : null, server: r.result.latency, firstBotAudioMs: r.firstBotAudioMs }));
  mkdirSync(resolve(ROOT, "e2e/out"), { recursive: true });
  writeFileSync(resolve(ROOT, `e2e/out/live-${Date.now()}.json`), JSON.stringify({
    gaps: r.gaps, result_latency: r.result.latency, usage: r.result.usage, cost, end_reason: r.result.end_reason,
    first_bot_audio_ms: r.firstBotAudioMs, create_ms: r.createMs, turns: r.result.turns.length,
    schedule_total_s: liveSchedule().total_s, splits, talk_over_ms: r.talkOverMs, llm_runs: r.llmRuns,
  }));
  expect(cost).toBeLessThanOrEqual(MAX_SPEND);
  expect(["completed", "ended_by_candidate", "time_limit"]).toContain(r.result.end_reason);
  expect(r.gaps.length).toBeGreaterThanOrEqual(10);
});
