// Spoken e2e harness: one real session through the running service, with the browser's fake microphone
// playing a generated session WAV (scripts/make_session_wav.py → e2e/out/).
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { chromium } from "@playwright/test";
import Ajv from "ajv";

export const BASE = process.env.E2E_BASE ?? "http://127.0.0.1:8770";
export const ROOT = resolve(import.meta.dirname, "..", "..");
export const MAX_SPEND = Number(process.env.E2E_MAX_SPEND_USD ?? "0.10");

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function readJson(rel: string): any {
  return JSON.parse(readFileSync(resolve(ROOT, rel), "utf8"));
}

const ajv = new Ajv({ strict: false, validateFormats: false, allErrors: true });
const resultSchema = ajv.compile(readJson("contracts/result.v1.schema.json"));
export function validateResult(r: unknown): boolean {
  const ok = resultSchema(r) as boolean;
  if (!ok) console.log(JSON.stringify(resultSchema.errors));
  return ok;
}

export async function health(): Promise<any> {
  return (await fetch(`${BASE}/health`)).json();
}

export type SessionRun = {
  result: any; gaps: number[]; firstBotAudioMs: number | null; captions: { user: number; bot: number }; createMs: number;
  talkOverMs: number; llmRuns: number | null;
};

// The service's own content-free "session_end ... llm_runs=N" log line for the session that just ended.
export function llmRunsFromLogs(sinceIso: string): number | null {
  // The service logs to stderr; read both streams.
  const r = spawnSync("docker", ["logs", "--since", sinceIso, process.env.E2E_CONTAINER ?? "iassistpro-interviewer-1"],
                      { encoding: "utf8" });
  const m = [...`${r.stdout ?? ""}${r.stderr ?? ""}`.matchAll(/session_end reason=\S+ turns=\d+ llm_runs=(\d+)/g)];
  return m.length ? Number(m[m.length - 1][1]) : null;
}

export async function runSession(opts: { brief: any; wav: string; durS: number; channel: string; endAfterS?: number }): Promise<SessionRun> {
  const t0 = Date.now();
  const sinceIso = new Date(t0 - 2000).toISOString();
  const created = await fetch(`${BASE}/sessions`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(opts.brief),
  });
  if (created.status !== 201) throw new Error(`create failed: ${created.status} ${await created.text()}`);
  const { session_id: id, connect } = await created.json();
  const createMs = Date.now() - t0;
  const browser = await chromium.launch({
    headless: true,
    ...(opts.channel !== "chromium" ? { channel: opts.channel } : {}),
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${resolve(opts.wav)}%noloop`,
      "--autoplay-policy=no-user-gesture-required",
    ],
  });
  let ender: NodeJS.Timeout | undefined;
  try {
    const page = await browser.newPage();
    page.on("console", (m) => { if (m.text().startsWith("[iassist]")) console.log(m.text()); });
    const url = `${BASE}/dev/client?session=${id}&token=${encodeURIComponent(connect.token)}&dur=${opts.durS}`;
    await page.goto(url);
    // The client module comes from esm.sh; if its import stalls, the page never starts: reload it once.
    try {
      await page.waitForFunction(() => ((window as any).iassist?.log ?? []).length > 0, null, { timeout: 30_000 });
    } catch {
      console.log("[harness] client did not start in 30 s; reloading the page once");
      await page.goto(url);
    }
    if (opts.endAfterS) ender = setTimeout(() => { fetch(`${BASE}/sessions/${id}/end`, { method: "POST" }); }, opts.endAfterS * 1000);
    await page.waitForFunction(() => (window as any).iassist?.done, null, { timeout: (opts.durS + 90) * 1000, polling: 500 });
    const browserSide = await page.evaluate(() => {
      const s = (window as any).iassist;
      return { gaps: s.gaps, firstBotAudioMs: s.firstBotAudioMs, captions: s.captions, talkOverMs: s.talkOverMs };
    });
    // The page left (dur) or the server ended the call; make sure the session finishes either way.
    let status: any;
    for (let i = 0; i < 60; i++) {
      status = await (await fetch(`${BASE}/sessions/${id}`)).json();
      if (status.state === "ended" || status.state === "failed") break;
      if (i === 5) await fetch(`${BASE}/sessions/${id}/end`, { method: "POST" });
      await sleep(1000);
    }
    for (let i = 0; i < 120; i++) {
      const a = await fetch(`${BASE}/sessions/${id}/audio`);
      if (a.status !== 409) break;
      await sleep(1000);
    }
    const result = await (await fetch(`${BASE}/sessions/${id}/result`)).json();
    return { result, createMs, ...browserSide, llmRuns: llmRunsFromLogs(sinceIso) };
  } finally {
    if (ender) clearTimeout(ender);
    await browser.close();
    await fetch(`${BASE}/sessions/${id}`, { method: "DELETE" });
  }
}

export function costUsd(result: any, p: { input: number; cached_input: number; cache_write: number; output: number }): number {
  let total = 0;
  for (const u of result.usage) {
    if ((u.kind ?? "llm") !== "llm") continue;
    total += u.input_tokens * p.input + u.cached_input_tokens * p.cached_input + u.cache_write_tokens * p.cache_write
      + u.output_tokens * p.output;
  }
  return total / 1e6;
}

export function p90(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.max(0, Math.round(0.9 * s.length) - 1)];
}

export function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// The spike's normaliser and word error rate.
const NUM: Record<string, string> = { six: "6", two: "2", ten: "10", four: "4", three: "3", ninety: "90", forty: "40", twice: "twice" };
export function normalize(text: string): string[] {
  return text.toLowerCase().replace(/-/g, " ").replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean).map((w) => NUM[w] ?? w);
}

export function wer(ref: string, hyp: string): number {
  const r = normalize(ref), h = normalize(hyp);
  const d = Array.from({ length: h.length + 1 }, (_, j) => j);
  for (let i = 1; i <= r.length; i++) {
    let prev = d[0];
    d[0] = i;
    for (let j = 1; j <= h.length; j++) {
      const cur = d[j];
      d[j] = Math.min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] !== h[j - 1] ? 1 : 0));
      prev = cur;
    }
  }
  return d[h.length] / Math.max(1, r.length);
}

// Split answers: each scripted answer must be exactly one candidate turn, in order, and match its clip's words.
export function splitCount(result: any, clips: { text: string }[]): number {
  const cand = result.turns.filter((t: any) => t.speaker === "candidate");
  let bad = Math.abs(cand.length - clips.length);
  for (let i = 0; i < Math.min(cand.length, clips.length); i++) if (wer(clips[i].text, cand[i].text) > 0.3) bad++;
  return bad;
}

export function candidateText(result: any): string {
  return result.turns.filter((t: any) => t.speaker === "candidate").map((t: any) => t.text).join(" ");
}
