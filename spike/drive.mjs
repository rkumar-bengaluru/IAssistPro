// Drive the spike client with Playwright + a fake microphone fed from a WAV file.
// usage: node drive.mjs <t=webrtc|ws> <dur_s> <wav> [channel=chromium|chrome|msedge] [port=8765]
import { createRequire } from "node:module";
const require = createRequire("C:/2026/rkumar/JobLens/joblens/web/package.json");
const { chromium } = require("@playwright/test");

const [kind = "webrtc", dur = "60", wav, channel = "chromium", port = "8765"] = process.argv.slice(2);
const args = [
  "--use-fake-ui-for-media-stream",
  "--use-fake-device-for-media-stream",
  `--use-file-for-fake-audio-capture=${wav}%noloop`,
  "--autoplay-policy=no-user-gesture-required",
];
const browser = await chromium.launch({
  headless: true, args, ...(channel !== "chromium" ? { channel } : {}),
});
const page = await browser.newPage();
page.on("console", (m) => { const t = m.text(); if (t.startsWith("[spike]")) console.log(t); });
page.on("pageerror", (e) => console.log("pageerror", e.message));
await page.goto(`http://127.0.0.1:${port}/?t=${kind}&dur=${dur}`);
await page.waitForFunction(() => window.spike && window.spike.done, null, { timeout: (Number(dur) + 60) * 1000 });
const res = await page.evaluate(() => ({ gaps: window.spike.gaps, ua: navigator.userAgent }));
console.log(JSON.stringify(res));
await browser.close();
