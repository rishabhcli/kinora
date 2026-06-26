import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import fs from "node:fs";
import path from "node:path";

// Scans the REAL app screens the DoD names — login → library → reading room —
// in demo mode (LoginPage enters even when the backend is unreachable; Home/Library
// render static demo books; the reading room shows placeholder text). Full reports
// are written for findings; the assertion is scoped to Agent 06's owned DOM on each
// screen (the app-wide skip link), since library/reading-room chrome belongs to
// Agents 5/10. Owned reading-room *surfaces* (ReadingControls/ReadAloudView) are
// covered by a11y.spec.ts once Agent 10 mounts them.

const ARTIFACTS = path.resolve(process.cwd(), "../../coordination/artifacts/agent-08");
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const isSerious = (i: string | null | undefined) => i === "serious" || i === "critical";

async function fullScan(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  fs.mkdirSync(ARTIFACTS, { recursive: true });
  fs.writeFileSync(
    path.join(ARTIFACTS, `axe-${name}.json`),
    JSON.stringify(
      {
        url: page.url(),
        screen: name,
        scannedAt: new Date().toISOString(),
        counts: {
          violations: results.violations.length,
          serious: results.violations.filter((v) => isSerious(v.impact)).length,
        },
        violations: results.violations.map((v) => ({
          id: v.id,
          impact: v.impact,
          help: v.help,
          nodes: v.nodes.length,
          targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
        })),
      },
      null,
      2,
    ),
  );
  return results;
}

async function ownedSkipLinkClean(page: Page, name: string) {
  const res = await new AxeBuilder({ page }).include(".skip-link").withTags(WCAG).analyze();
  const serious = res.violations.filter((v) => isSerious(v.impact));
  expect(serious, `owned (.skip-link) on ${name}: ${JSON.stringify(serious)}`).toEqual([]);
}

async function enterDemo(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: /^sign in$/i }).click();
  await expect(page.getByRole("button", { name: "Library" }).first()).toBeVisible({ timeout: 15_000 });
}

test("home screen (demo books): scan + owned clean", async ({ page }) => {
  await enterDemo(page);
  await fullScan(page, "app-home");
  await ownedSkipLinkClean(page, "home");
});

// NOTE: the LibraryPage scan runs via the harness (e2e/harness/library.html) in
// a11y.spec.ts — the live in-app page switch (framer dock/header swap +
// AnimatePresence crossfade) is unreliable headless. The harness mounts the REAL
// LibraryPage component, so the scan is genuine.

test("reading room: scan + owned clean", async ({ page }) => {
  await enterDemo(page);
  await page.locator(".book-cover").first().click();
  await expect(page.getByRole("dialog")).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(800); // let the entrance settle + text mount
  await fullScan(page, "app-reading-room");
  await ownedSkipLinkClean(page, "reading-room");
});
