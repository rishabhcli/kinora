// Reusable axe-core audit helper. The existing e2e/a11y.spec.ts scans owned
// surfaces; this wrapper lets the broader screen specs run the *same* WCAG
// ruleset and write per-screen reports, with a configurable severity gate so we
// can scan a screen for findings without failing on chrome owned by other agents.

import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

export const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

export type Impact = "minor" | "moderate" | "serious" | "critical";

export interface AxeFinding {
  id: string;
  impact: Impact | null;
  help: string;
  nodes: number;
  targets: string[];
}

export interface AxeReport {
  url: string;
  screen: string;
  scannedAt: string;
  counts: { total: number; serious: number };
  violations: AxeFinding[];
}

const isSerious = (i: Impact | null | undefined) => i === "serious" || i === "critical";

/** Run an axe scan over `include` (or the whole page) and return a structured report. */
export async function audit(
  page: Page,
  screen: string,
  include?: string,
): Promise<AxeReport> {
  let builder = new AxeBuilder({ page }).withTags(WCAG_TAGS);
  if (include) builder = builder.include(include);
  const results = await builder.analyze();
  const violations: AxeFinding[] = results.violations.map((v) => ({
    id: v.id,
    impact: v.impact as Impact | null,
    help: v.help,
    nodes: v.nodes.length,
    targets: v.nodes.slice(0, 6).map((n) => n.target.join(" ")),
  }));
  return {
    url: page.url(),
    screen,
    scannedAt: new Date().toISOString(),
    counts: { total: violations.length, serious: violations.filter((v) => isSerious(v.impact)).length },
    violations,
  };
}

/** Serious + critical findings only. */
export function seriousOnly(report: AxeReport): AxeFinding[] {
  return report.violations.filter((v) => isSerious(v.impact));
}

/** Write the report as a CI artifact (attachment + shared artifacts dir). */
export function writeReport(testInfo: TestInfo, report: AxeReport): void {
  const body = Buffer.from(JSON.stringify(report, null, 2));
  testInfo.attachments.push({
    name: `axe-${report.screen}.json`,
    contentType: "application/json",
    body,
  });
  try {
    const dir = path.resolve(process.cwd(), "../../coordination/artifacts/e2e-a11y");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `axe-${report.screen}.json`), body);
  } catch {
    /* optional */
  }
}

/** Compact one-line summary for assertion messages. */
export function summarize(findings: AxeFinding[]): string {
  return JSON.stringify(
    findings.map((f) => ({ id: f.id, impact: f.impact, nodes: f.nodes })),
    null,
    2,
  );
}
