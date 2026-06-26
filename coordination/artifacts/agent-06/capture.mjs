import { chromium } from "playwright-core";
import { mkdirSync, renameSync, readdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const EXEC = join(
  homedir(),
  "Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
);
const URL = process.env.KINORA_URL || "http://127.0.0.1:5204/";
const OUT = "/Users/m3-max/Documents/GitHub/kinora-a04/coordination/artifacts/agent-06";
const VW = 1440, VH = 900;
mkdirSync(OUT, { recursive: true });

const report = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const FPS_START = () => {
  window.__f = []; window.__on = true;
  const loop = (t) => { if (!window.__on) return; window.__f.push(t); requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
};
const FPS_STOP = () => {
  window.__on = false;
  const f = window.__f || [];
  const iv = [];
  for (let i = 1; i < f.length; i++) iv.push(f[i] - f[i - 1]);
  if (!iv.length) return { frames: f.length, fps: 0, avgMs: 0, p95Ms: 0, maxMs: 0, longFrames: 0 };
  const sorted = [...iv].sort((a, b) => a - b);
  const n = sorted.length;
  const avg = iv.reduce((a, b) => a + b, 0) / n;
  return {
    frames: f.length,
    fps: +(1000 / avg).toFixed(1),
    avgMs: +avg.toFixed(2),
    p95Ms: +(sorted[Math.floor(n * 0.95)] || 0).toFixed(2),
    maxMs: +(sorted[n - 1] || 0).toFixed(2),
    longFrames: iv.filter((x) => x > 20).length,
  };
};

async function shot(page, name) {
  await page.screenshot({ path: join(OUT, name) });
  console.log("  shot:", name);
}

async function enterApp(page) {
  await page.goto(URL, { waitUntil: "load" });
  await sleep(900);
  // Sign in (demo creds are pre-filled; backend is offline so enter() falls
  // through to demo mode). Poll for the home shelf, nudging the demo button
  // if the submit didn't land.
  await page.locator('button[type="submit"]').first().click({ timeout: 5000 }).catch(() => {});
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    const n = await page.locator(".book-cover").count();
    if (n > 0) break;
    await page.getByText("Explore the demo library", { exact: false }).first().click({ timeout: 1500 }).catch(() => {});
    await sleep(800);
  }
  await page.locator(".book-cover").first().waitFor({ state: "visible", timeout: 10000 });
  await sleep(1100); // let entrance staggers settle
}

async function flingRail(page) {
  // Physics drag on the ShelfScroller rail: down, quick moves left, up.
  const rail = page.locator(".mo-shelf-rail").first();
  const box = await rail.boundingBox();
  if (!box) return;
  const y = box.y + box.height / 2;
  const startX = box.x + box.width * 0.8;
  await page.mouse.move(startX, y);
  await page.mouse.down();
  for (let i = 1; i <= 8; i++) {
    await page.mouse.move(startX - i * 70, y, { steps: 1 });
    await sleep(8);
  }
  await page.mouse.up(); // releases into momentum + snap
}

async function normalPass() {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({
    viewport: { width: VW, height: VH },
    deviceScaleFactor: 1,
    recordVideo: { dir: join(OUT, "_vid_normal"), size: { width: VW, height: VH } },
  });
  const page = await ctx.newPage();
  const cover = () => page.locator(".book-cover").first();
  try {
    await enterApp(page);
    await shot(page, "01-home-shelf.png");

    // — PAGE TRANSITION — click Library nav
    await page.getByText("Library", { exact: true }).first().click().catch(() => {});
    await sleep(140);
    await shot(page, "06-page-transition.png");
    await page.getByText("Home", { exact: true }).first().click().catch(() => {});
    await cover().waitFor({ state: "visible", timeout: 6000 });
    await sleep(500);

    // — BOOK OPEN — visual frames (travel → settle → reveal) —
    await cover().click();
    await sleep(150); await shot(page, "02a-open-liftoff.png");
    await sleep(320); await shot(page, "02b-open-travel.png");
    await sleep(450); await shot(page, "02c-open-settle.png");
    await sleep(600); await shot(page, "02d-open-room.png");
    // — BOOK CLOSE — visual frames —
    await sleep(300);
    await page.keyboard.press("Escape");
    await sleep(160); await shot(page, "03a-close-collapse.png");
    await sleep(360); await shot(page, "03b-close-return.png");
    await sleep(700); await shot(page, "03c-closed-shelf.png");
    await cover().waitFor({ state: "visible", timeout: 6000 });
    await sleep(500);

    // — CLEAN FPS — no screenshots inside the sampling windows (CDP-noise free) —
    await page.evaluate(FPS_START);
    await cover().click();
    await sleep(1500); // travel + settle + reveal
    report.bookOpen = await page.evaluate(FPS_STOP);
    console.log("  bookOpen fps:", JSON.stringify(report.bookOpen));

    await sleep(400);
    await page.evaluate(FPS_START);
    await page.keyboard.press("Escape");
    await sleep(1300); // close flight
    report.bookClose = await page.evaluate(FPS_STOP);
    console.log("  bookClose fps:", JSON.stringify(report.bookClose));
  } finally {
    await ctx.close();
    await browser.close();
  }
  renameVid(join(OUT, "_vid_normal"), "video-book-open-close.webm");
}

async function showcasePass() {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({
    viewport: { width: VW, height: VH },
    deviceScaleFactor: 1,
    recordVideo: { dir: join(OUT, "_vid_shelf"), size: { width: VW, height: VH } },
  });
  const page = await ctx.newPage();
  try {
    // Load the showcase URL FIRST, then sign in — the in-app login→home
    // transition preserves the ?motiondemo query (a reload would reset auth).
    await page.goto(URL + "?motiondemo=1", { waitUntil: "load" });
    await sleep(900);
    await page.locator('button[type="submit"]').first().click({ timeout: 5000 }).catch(() => {});
    const deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      if (await page.locator(".mo-shelf-rail").count()) break;
      await page.getByText("Explore the demo library", { exact: false }).first().click({ timeout: 1500 }).catch(() => {});
      await sleep(800);
    }
    await page.locator(".mo-shelf-rail").first().waitFor({ state: "visible", timeout: 10000 });
    await sleep(1000);
    await shot(page, "04a-shelf-rest.png");

    // — TILT — hover a cover
    const c = page.locator(".mo-shelf-rail .book-cover").nth(2);
    const cb = await c.boundingBox();
    if (cb) { await page.mouse.move(cb.x + cb.width * 0.7, cb.y + cb.height * 0.3); await sleep(250); }
    await shot(page, "05-tilt-hover.png");

    // — SHELF SCROLL — visual frames —
    await flingRail(page);
    await sleep(120); await shot(page, "04b-shelf-flinging.png");
    await sleep(800); await shot(page, "04c-shelf-settled.png");

    // reset to the start, then a CLEAN FPS fling (no screenshots in window)
    await page.evaluate(() => { const r = document.querySelector(".mo-shelf-rail"); if (r) r.scrollLeft = 0; });
    await sleep(400);
    await page.evaluate(FPS_START);
    await flingRail(page);
    await sleep(1100);
    report.shelfScroll = await page.evaluate(FPS_STOP);
    console.log("  shelfScroll fps:", JSON.stringify(report.shelfScroll));
  } finally {
    await ctx.close();
    await browser.close();
  }
  renameVid(join(OUT, "_vid_shelf"), "video-shelf-scroll.webm");
}

async function reducedPass() {
  const browser = await chromium.launch({ executablePath: EXEC, headless: true });
  const ctx = await browser.newContext({
    viewport: { width: VW, height: VH },
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
    recordVideo: { dir: join(OUT, "_vid_reduced"), size: { width: VW, height: VH } },
  });
  const page = await ctx.newPage();
  try {
    await enterApp(page);
    await shot(page, "07a-reduced-home.png");
    // Reduced motion: the morph is skipped entirely — the room appears with a
    // clean fade (no travel, no hinge-driven transform). Screenshots prove it.
    await page.locator(".book-cover").first().click();
    await sleep(120); await shot(page, "07b-reduced-open-instant.png");
    await sleep(500); await shot(page, "07c-reduced-room.png");
    await page.keyboard.press("Escape");
    await sleep(500); await shot(page, "07d-reduced-closed.png");
  } finally {
    await ctx.close();
    await browser.close();
  }
  renameVid(join(OUT, "_vid_reduced"), "video-reduced-motion.webm");
}

function renameVid(dir, name) {
  try {
    const files = readdirSync(dir).filter((f) => f.endsWith(".webm"));
    if (files[0]) renameSync(join(dir, files[0]), join(OUT, name));
    console.log("  video:", name);
  } catch (e) {
    console.log("  video rename skipped:", e.message);
  }
}

(async () => {
  console.log("EXEC:", EXEC);
  console.log("normal pass…"); await normalPass();
  console.log("showcase pass…"); await showcasePass();
  console.log("reduced pass…"); await reducedPass();
  report._meta = { url: URL, viewport: `${VW}x${VH}`, capturedAt: new Date().toISOString(),
    note: "fps measured via requestAnimationFrame frame intervals during each interaction in Chrome for Testing (headless, GPU-composited)." };
  writeFileSync(join(OUT, "fps-report.json"), JSON.stringify(report, null, 2));
  console.log("DONE. report:", JSON.stringify(report, null, 2));
})().catch((e) => { console.error("CAPTURE FAILED:", e); process.exit(1); });
