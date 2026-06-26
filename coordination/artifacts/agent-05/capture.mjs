// Capture Agent-05 library + upload screenshots (standalone Playwright/chromium).
// Run from apps/desktop: node _a05_capture.mjs
import { chromium } from "@playwright/test";
import { writeFileSync } from "node:fs";

const WEB = "http://localhost:5173";
const OUT = "/Users/m3-max/Documents/GitHub/kinora-a05/coordination/artifacts/agent-05";
const EPUB = "/Users/m3-max/Documents/GitHub/kinora-a05/assets/books/public-domain/pg1952.epub";
const log = [];

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 980 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();
await page.goto(WEB, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(1200);

async function enter() {
  const demo = page.getByText(/Explore the demo library/i).first();
  if (await demo.count().catch(() => 0)) {
    await demo.click().catch(() => {});
    return "demo-link";
  }
  await page.locator('input[type="email"]').first().fill("demo@kinora.local").catch(() => {});
  await page.locator('input[type="password"]').first().fill("demo-password-123").catch(() => {});
  await page.getByRole("button", { name: /sign in/i }).first().click().catch(() => {});
  return "form";
}
log.push("enter via " + (await enter()));
await page.waitForTimeout(3500);

for (const name of [/^Library$/i, /Library/i, /My Library/i]) {
  const el = page.getByRole("button", { name }).or(page.getByRole("link", { name })).first();
  if (await el.count().catch(() => 0)) { await el.click().catch(() => {}); break; }
  const t = page.getByText(name).first();
  if (await t.count().catch(() => 0)) { await t.click().catch(() => {}); break; }
}
await page.waitForTimeout(2000);
await page.waitForLoadState("networkidle").catch(() => {});
await page.waitForTimeout(3000);

await page.screenshot({ path: `${OUT}/01-library.png` });
await page.screenshot({ path: `${OUT}/01-library-full.png`, fullPage: true });

const counts = await page.evaluate(() => ({
  cards: document.querySelectorAll('[role="button"][aria-label]').length,
  imgs: document.querySelectorAll("img").length,
  h1: document.querySelector("h1")?.textContent || "",
}));
log.push(`library: cards=${counts.cards} imgs=${counts.imgs} h1="${counts.h1}"`);

try {
  await page.locator('input[type="file"]').first().setInputFiles(EPUB, { timeout: 5000 });
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${OUT}/02-upload.png` });
  log.push("upload: captured");
} catch (e) {
  log.push("upload: FAILED " + e);
}

writeFileSync(`${OUT}/capture-note.txt`, log.join("\n") + "\n");
console.log(log.join("\n"));
await browser.close();
