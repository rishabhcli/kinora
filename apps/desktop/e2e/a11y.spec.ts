import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import fs from "node:fs";
import path from "node:path";

// Automated axe-core scan (WCAG 2.0/2.1/2.2 A + AA). Owned surfaces (the a11y
// harness + the cheat-sheet) must report ZERO serious/critical. The real login
// screen is scanned too; its full report is written for findings.

const ARTIFACTS = path.resolve(process.cwd(), "../../coordination/artifacts/agent-08");
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

type Impact = "minor" | "moderate" | "serious" | "critical";
const isSerious = (i: Impact | null | undefined) => i === "serious" || i === "critical";

async function scan(page: Page, name: string, include?: string) {
  let builder = new AxeBuilder({ page }).withTags(WCAG);
  if (include) builder = builder.include(include);
  const results = await builder.analyze();
  fs.mkdirSync(ARTIFACTS, { recursive: true });
  fs.writeFileSync(
    path.join(ARTIFACTS, `axe-${name}.json`),
    JSON.stringify(
      {
        url: page.url(),
        scannedAt: new Date().toISOString(),
        counts: {
          violations: results.violations.length,
          serious: results.violations.filter((v) => isSerious(v.impact as Impact)).length,
        },
        violations: results.violations.map((v) => ({
          id: v.id,
          impact: v.impact,
          help: v.help,
          nodes: v.nodes.length,
          detail: v.nodes.slice(0, 8).map((n) => ({
            target: n.target.join(" "),
            html: n.html,
            summary: n.failureSummary,
          })),
        })),
      },
      null,
      2,
    ),
  );
  return results.violations.filter((v) => isSerious(v.impact as Impact));
}

function summarize(violations: Awaited<ReturnType<typeof scan>>) {
  return JSON.stringify(
    violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length })),
    null,
    2,
  );
}

test("owned reading surfaces (harness): zero serious/critical", async ({ page }) => {
  await page.goto("/e2e/harness/index.html");
  await expect(page.getByRole("group", { name: /reading settings/i })).toBeVisible();
  const serious = await scan(page, "owned-reading-surfaces");
  expect(serious, summarize(serious)).toEqual([]);
});

test("owned surface — keyboard shortcuts cheat-sheet: zero serious/critical", async ({ page }) => {
  await page.goto("/e2e/harness/index.html");
  await page.keyboard.press("Shift+Slash"); // "?" opens the cheat-sheet
  await expect(page.getByRole("dialog", { name: /keyboard shortcuts/i })).toBeVisible();
  const serious = await scan(page, "owned-cheatsheet", '[role="dialog"]');
  expect(serious, summarize(serious)).toEqual([]);
});

test("library (real LibraryPage via harness): report + owned clean", async ({ page }) => {
  await page.goto("/e2e/harness/library.html");
  await expect(page.getByRole("heading", { name: /my library/i })).toBeVisible();
  await scan(page, "app-library"); // full report for findings (Agent 5 chrome)
  const ownedSerious = await scan(page, "app-library-owned", ".skip-link");
  expect(ownedSerious, summarize(ownedSerious)).toEqual([]);
});

test("login screen: report violations; owned additions clean", async ({ page }) => {
  await page.goto("/");
  // Scan the whole login screen for the findings report...
  await scan(page, "login-full");
  // ...but only assert on the skip link Agent 06 injects app-wide.
  const serious = await scan(page, "login-owned", ".skip-link");
  expect(serious, summarize(serious)).toEqual([]);
});
