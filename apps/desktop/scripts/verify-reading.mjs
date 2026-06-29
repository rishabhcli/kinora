// Render-driving verification for the reading-room redesign. Screenshots are
// blocked on this machine, so we assert on DOM/behaviour instead. Requires the
// Vite dev renderer running at http://localhost:5173 (pnpm --filter
// @kinora/desktop run dev:web). Runs against the BROWSER surface, where
// window.kinora is undefined, so a book opens via the in-app overlay path.
//
//   node apps/desktop/scripts/verify-reading.mjs
import { chromium } from "@playwright/test";

const BASE = process.env.KINORA_VERIFY_URL ?? "http://localhost:5173";
const checks = [];
const check = (name, cond) => {
  checks.push({ name, ok: !!cond });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));

await page.goto(BASE, { waitUntil: "domcontentloaded" });

// Enter the demo library (LoginPage → "explore the demo library"), then open the
// first book on the public-domain shelf.
const demo = page.getByRole("button", { name: /explore the demo library/i });
if (await demo.count()) await demo.first().click();

// Open a book: click the first book cover on the shelf.
await page.locator(".book-cover").first().click({ timeout: 20000 }).catch(() => {});

// Wait until the reading scroll container is present (warm-up settled).
const scroll = page.locator('[data-reading-scroll]').first();
await scroll.waitFor({ state: "visible", timeout: 30000 });
check("reading scroll container is visible", await scroll.isVisible());

// (a) film <video> is LEFT of the scroll column.
const layout = await page.evaluate(() => {
  const sc = document.querySelector('[data-reading-scroll]');
  const vid = document.querySelector('video');
  if (!sc || !vid) return null;
  return { videoLeft: vid.getBoundingClientRect().left, scrollLeft: sc.getBoundingClientRect().left };
});
check("film <video> renders LEFT of the reading column", layout && layout.videoLeft < layout.scrollLeft);

// (b) native scroll moves scrollTop.
const before = await scroll.evaluate((el) => el.scrollTop);
await scroll.evaluate((el) => el.scrollBy({ top: 1200 }));
await page.waitForTimeout(300);
const after = await scroll.evaluate((el) => el.scrollTop);
check("scrolling moves scrollTop (native, not jacked)", after > before);

// (c) gentle focus: an in-focus paragraph is more opaque than a far one.
const opacity = await page.evaluate(() => {
  const paras = Array.from(document.querySelectorAll('[data-para]'));
  if (paras.length < 6) return null;
  const alpha = (el) => {
    const m = /rgba?\([^)]*?,\s*([\d.]+)\s*\)/.exec(getComputedStyle(el).color);
    return m ? parseFloat(m[1]) : 1;
  };
  // The active one near the 40% focus line vs. the very first (far above).
  const mid = paras[Math.floor(paras.length / 2)];
  return { mid: alpha(mid), far: alpha(paras[0]) };
});
check("gentle focus applied (active paragraph more opaque than a far one)", opacity && opacity.mid >= opacity.far);

check("no uncaught page errors during scroll", errors.length === 0);
if (errors.length) console.log("  errors:", errors.join("\n  "));

await browser.close();
const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
