import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const EXEC =
  "/Users/m3-max/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing";
const BASE = process.env.BASE || "http://localhost:5174";
const ART = "/Users/m3-max/Documents/GitHub/kinora-a10/coordination/artifacts/agent-12";
mkdirSync(ART, { recursive: true });

const browser = await chromium.launch({
  executablePath: EXEC,
  headless: true,
  args: ["--autoplay-policy=no-user-gesture-required"],
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const allErrors = {};
const failed404 = new Set();

async function newCtx(name) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 832 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs = [];
  allErrors[name] = errs;
  page.on("console", (m) => {
    if (m.type() === "error") errs.push(m.text());
  });
  page.on("pageerror", (e) => errs.push("PAGEERROR: " + e.message));
  page.on("response", (r) => {
    if (r.status() === 404) failed404.add(new URL(r.url()).pathname);
  });
  return { ctx, page };
}

// Instrumentation: count EventSource open/close + net document keydown listeners.
function instrument() {
  window.__esOpened = 0;
  window.__esClosed = 0;
  window.__keydown = 0;
  const add = document.addEventListener.bind(document);
  const rem = document.removeEventListener.bind(document);
  document.addEventListener = (t, ...a) => {
    if (t === "keydown") window.__keydown++;
    return add(t, ...a);
  };
  document.removeEventListener = (t, ...a) => {
    if (t === "keydown") window.__keydown--;
    return rem(t, ...a);
  };
}

// In-page backend mock (fetch + EventSource), installed before app scripts run.
function installMock(cfg) {
  try {
    localStorage.setItem("kinora.token", "mock-token");
  } catch {}
  const json = (obj, status = 200) =>
    Promise.resolve(new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } }));
  const real = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
    let m;
    if (path === "/api/books") return json([]);
    if ((m = path.match(/^\/api\/books\/[^/]+$/))) return json(cfg.meta);
    if ((m = path.match(/^\/api\/books\/[^/]+\/pages\/(\d+)$/))) {
      const n = +m[1];
      return json({ book_id: cfg.meta.id, page_number: n, image_url: null, text: cfg.pages[n - 1] ?? null, word_boxes: null });
    }
    if (path.match(/^\/api\/books\/[^/]+\/shots$/)) {
      const body = JSON.stringify(cfg.shots || []);
      if (cfg.shotsDelayMs)
        return new Promise((res) =>
          setTimeout(() => res(new Response(body, { status: 200, headers: { "Content-Type": "application/json" } })), cfg.shotsDelayMs),
        );
      return Promise.resolve(new Response(body, { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    if (path === "/api/sessions")
      return json({ session_id: "sess-mock", book_id: cfg.meta.id, focus_word: 0, velocity_wps: 0, committed_seconds_ahead: 0, bursting: false, budget_remaining_s: null });
    if (/\/api\/sessions\/[^/]+\/(intent|seek|comment)$/.test(path)) return json({});
    return real(input, init); // local assets (films/covers) pass through
  };

  class FakeES {
    constructor(url) {
      window.__esOpened++;
      this.url = url;
      this.listeners = {};
      this.onmessage = null;
      this.readyState = 1;
      (cfg.sse || []).forEach((ev) => {
        setTimeout(() => {
          if (this.readyState === 2) return;
          const me = new MessageEvent(ev.data.event, { data: JSON.stringify(ev.data) });
          (this.listeners[ev.data.event] || []).forEach((fn) => fn(me));
        }, ev.at || 0);
      });
    }
    addEventListener(name, fn) {
      (this.listeners[name] = this.listeners[name] || []).push(fn);
    }
    removeEventListener() {}
    close() {
      this.readyState = 2;
      window.__esClosed++;
    }
  }
  window.EventSource = FakeES;
}

// True offline: no token + every /api/* call rejects (like a backend that isn't
// running). Avoids the dev-only :8000-CORS confound on this alt port.
function installOffline() {
  try {
    localStorage.removeItem("kinora.token");
  } catch {}
  const real = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (url.includes("/api/")) return Promise.reject(new TypeError("offline"));
    return real(input, init);
  };
}

async function enterDemo(page) {
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: /Explore the demo library/i }).click();
  await page.waitForTimeout(900);
}

async function openBook(page, title) {
  const clicked = await page.evaluate((t) => {
    const card = [...document.querySelectorAll("div.cursor-pointer")].find((c) => (c.textContent || "").includes(t));
    if (!card) return false;
    card.click();
    return true;
  }, title);
  if (!clicked) throw new Error(`book card "${title}" not found`);
  await page.waitForSelector('[role="dialog"]', { timeout: 6000 });
}

const filmState = (page) =>
  page.evaluate(() => {
    const v = document.querySelector("video");
    return v ? { readyState: v.readyState, t: +v.currentTime.toFixed(2), w: v.videoWidth, paused: v.paused, err: v.error?.code ?? null } : null;
  });

// ---- Scenario 1: no-backend fallback + close --------------------------------
async function scenarioNoBackend() {
  const { ctx, page } = await newCtx("no-backend");
  await page.addInitScript(instrument);
  await page.addInitScript(installOffline); // truly offline → immediate fallback
  await enterDemo(page);
  await page.screenshot({ path: `${ART}/00-home.png` });
  await openBook(page, "Dune");
  await sleep(480);
  await page.screenshot({ path: `${ART}/nobackend-01-opening.png` });
  await sleep(1300);
  await page.screenshot({ path: `${ART}/nobackend-02-playing.png` });
  await page.locator("[data-reading-scroll]").evaluate((el) => el.scrollTo({ top: 1200 }));
  await sleep(450);
  await page.screenshot({ path: `${ART}/nobackend-03-scrolled.png` });
  const film = await filmState(page);
  await page.getByRole("button", { name: /Close reader and go back/i }).click();
  await sleep(420); // mid cover-flip-shut
  await page.screenshot({ path: `${ART}/close-01-closing.png` });
  // The close choreography (cover swings shut ~0.95s) — wait it out, then confirm.
  await page.waitForFunction(() => !document.querySelector('[role="dialog"]'), null, { timeout: 3000 });
  const stillOpen = await page.evaluate(() => !!document.querySelector('[role="dialog"]'));
  await ctx.close();
  return { film, closedCleanly: !stillOpen };
}

// ---- Scenario 2: ready backend book (live path, real clips) -----------------
async function scenarioReady() {
  const { ctx, page } = await newCtx("ready");
  const cfg = {
    meta: { id: "dune", title: "Dune", author: "Frank Herbert", status: "ready", num_pages: 3, progress: 0.1, stage: "ready", art_direction: null, created_at: null },
    pages: [
      "Paul Atreides woke before dawn, the desert wind speaking in a tongue older than the great houses.",
      "Beyond the window, the dunes rolled to the horizon like a sea that had forgotten how to move.",
      "And in the silence between heartbeats, the spice whispered of a future only he could see.",
    ],
    shots: [
      { shot_id: "s1", status: "ready", duration_s: 4, clip_url: "/generated/film-01.mp4", source_span: { word_range: [0, 100] } },
      { shot_id: "s2", status: "ready", duration_s: 4, clip_url: "/generated/film-03.mp4", source_span: { word_range: [100, 220] } },
      { shot_id: "s3", status: "rendering", duration_s: 4, clip_url: null, source_span: { word_range: [220, 360] } },
    ],
    sse: [
      { at: 300, data: { event: "agent_activity", agent: "Director", message: "Blocking the opening shot" } },
      { at: 700, data: { event: "buffer_state", committed_seconds_ahead: 12.4, bursting: false, idle: false, inflight_committed: 1, inflight_speculative: 2, zone: "committed" } },
      { at: 1500, data: { event: "clip_ready", shot_id: "s3", oss_url: "/generated/film-04.mp4" } },
    ],
  };
  await page.addInitScript(instrument);
  await page.addInitScript(installMock, cfg);
  await enterDemo(page);
  await openBook(page, "Dune");
  await sleep(1900);
  await page.screenshot({ path: `${ART}/ready-01-open.png` });
  await page.locator("[data-reading-scroll]").evaluate((el) => el.scrollTo({ top: 1400 }));
  await sleep(500);
  await page.screenshot({ path: `${ART}/ready-02-scrubbed.png` });
  const film = await filmState(page);
  const live = await page.evaluate(() => document.body.innerText.includes("ahead"));
  await ctx.close();
  return { film, livePill: live };
}

// ---- Scenario 3: mid-ingest (analyzing, shots delayed) → warm-up progress ----
async function scenarioMidIngest() {
  const { ctx, page } = await newCtx("mid-ingest");
  const cfg = {
    meta: { id: "midnight-library", title: "The Midnight Library", author: "Matt Haig", status: "ANALYZING", num_pages: 3, progress: 0.4, stage: "analyzing", art_direction: null, created_at: null },
    pages: ["Between life and death there is a library, and the shelves go on forever."],
    shotsDelayMs: 2200, // ingest still composing shots — lingers in loading
    shots: [{ shot_id: "s1", status: "ready", duration_s: 4, clip_url: "/generated/film-02.mp4", source_span: { word_range: [0, 120] } }],
    sse: [
      { at: 200, data: { event: "agent_activity", agent: "Set Designer", message: "Designing the midnight library" } },
      { at: 600, data: { event: "agent_activity", agent: "Cinematographer", message: "Lighting the endless shelves" } },
      { at: 1000, data: { event: "buffer_state", committed_seconds_ahead: 3.2, bursting: true, idle: false, inflight_committed: 2, inflight_speculative: 1, zone: "speculative" } },
    ],
  };
  await page.addInitScript(instrument);
  await page.addInitScript(installMock, cfg);
  await enterDemo(page);
  await openBook(page, "Midnight Library");
  await sleep(1450); // cover gone, content revealed, shots still loading → warm-up visible
  await page.screenshot({ path: `${ART}/midingest-01-warmup.png` });
  await sleep(1400); // shots arrive → reveal
  await page.screenshot({ path: `${ART}/midingest-02-revealed.png` });
  const film = await filmState(page);
  await ctx.close();
  return { film };
}

// ---- Scenario 4: teardown leak check (open/close 10×) -----------------------
async function scenarioLeak() {
  const { ctx, page } = await newCtx("leak");
  const cfg = {
    meta: { id: "dune", title: "Dune", author: "Frank Herbert", status: "ready", num_pages: 1, progress: 0, stage: "ready", art_direction: null, created_at: null },
    pages: ["A single page, opened and closed ten times over."],
    shots: [{ shot_id: "s1", status: "ready", duration_s: 4, clip_url: "/generated/film-01.mp4", source_span: { word_range: [0, 80] } }],
    sse: [{ at: 100, data: { event: "buffer_state", committed_seconds_ahead: 5, bursting: false, idle: false } }],
  };
  await page.addInitScript(instrument);
  await page.addInitScript(installMock, cfg);
  await enterDemo(page);
  const baselineKeydown = await page.evaluate(() => window.__keydown);
  for (let i = 0; i < 10; i++) {
    await openBook(page, "Dune");
    await sleep(260);
    await page.getByRole("button", { name: /Close reader and go back/i }).click();
    // Wait for the dialog to actually detach (exit animation + teardown) before
    // the next cycle — deterministic, not a fixed sleep.
    await page.waitForFunction(() => !document.querySelector('[role="dialog"]'), null, { timeout: 4000 });
  }
  const counters = await page.evaluate(() => ({
    esOpened: window.__esOpened,
    esClosed: window.__esClosed,
    keydownNet: window.__keydown,
    dialogs: document.querySelectorAll('[role="dialog"]').length,
    videos: document.querySelectorAll("video").length,
  }));
  await ctx.close();
  return { baselineKeydown, ...counters, keydownLeak: counters.keydownNet - baselineKeydown };
}

// ---- Scenario 5: rapid same-book close→reopen (P0 regression) ---------------
// Reopening the SAME book during the exit animation must still reveal (not freeze
// in the warm-up). Detected via the [data-warmup] overlay being gone after reopen.
async function scenarioRapidReopen() {
  const { ctx, page } = await newCtx("rapid-reopen");
  await page.addInitScript(installOffline);
  await enterDemo(page);
  const warmupGoes = () =>
    page
      .waitForFunction(() => !document.querySelector("[data-warmup]"), null, { timeout: 4500 })
      .then(() => true)
      .catch(() => false);

  await openBook(page, "Dune");
  const revealed1 = await warmupGoes(); // first open dismisses the warm-up
  // Close, then reopen the SAME book mid-exit (~150ms in, within the ~1s exit) so
  // AnimatePresence interrupts the exit and reuses the instance — the P0 case.
  await page.getByRole("button", { name: /Close reader and go back/i }).click();
  await sleep(150);
  await page.evaluate(() => {
    const card = [...document.querySelectorAll("div.cursor-pointer")].find((c) => (c.textContent || "").includes("Dune"));
    card?.click();
  });
  await page.waitForSelector('[role="dialog"]', { timeout: 6000 });
  const warmupGone = await warmupGoes(); // MUST dismiss again (not freeze)
  const rest = await page.evaluate(() => ({
    dialog: !!document.querySelector('[role="dialog"]'),
    videoPlaying: (() => { const v = document.querySelector("video"); return !!v && !v.paused && v.readyState >= 2; })(),
  }));
  await page.screenshot({ path: `${ART}/reopen-01-revealed.png` });
  await ctx.close();
  return { revealed1, warmupGone, ...rest };
}

const results = {};
for (const [name, fn] of [
  ["noBackend", scenarioNoBackend],
  ["ready", scenarioReady],
  ["midIngest", scenarioMidIngest],
  ["leak", scenarioLeak],
  ["rapidReopen", scenarioRapidReopen],
]) {
  try {
    results[name] = await fn();
  } catch (e) {
    results[name] = { ERROR: String(e).split("\n")[0] };
  }
}

await browser.close();
console.log(JSON.stringify({ results, failed404: [...failed404], errors: allErrors }, null, 2));
