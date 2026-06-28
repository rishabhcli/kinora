// Performance tracing helpers: collect FCP / LCP / TTFB / long-task jank from
// the browser's own Performance APIs, plus a Playwright trace toggle. These feed
// the perf spec (e2e/specs/perf.spec.ts), which asserts soft budgets so a
// regression in the renderer's startup cost is caught in CI without a full
// Lighthouse run.

import type { Page, TestInfo } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

export interface PerfMetrics {
  /** Time to first byte of the document (ms from navigation start). */
  ttfb: number | null;
  /** First Contentful Paint (ms). */
  fcp: number | null;
  /** Largest Contentful Paint (ms). */
  lcp: number | null;
  /** DOMContentLoaded (ms). */
  dcl: number | null;
  /** Full load event (ms). */
  load: number | null;
  /** Number of long tasks (>50ms) observed. */
  longTasks: number;
  /** Total blocking-ish time: sum of (longTask.duration - 50) over all long tasks. */
  totalLongTaskMs: number;
  /** Approx total JS heap used (bytes), Chromium-only. */
  jsHeapBytes: number | null;
}

/**
 * Install a PerformanceObserver before navigation so LCP/long-task entries are
 * captured from the very start. Reads paint + navigation timing on demand.
 * Chromium-only (the E2E project pins Desktop Chrome), which is also exactly the
 * engine the production Electron renderer runs.
 */
export async function startPerfObserver(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as {
      __perf: { lcp: number; longTasks: number; totalLongTaskMs: number };
    };
    w.__perf = { lcp: 0, longTasks: 0, totalLongTaskMs: 0 };
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          w.__perf.lcp = Math.max(w.__perf.lcp, e.startTime + (e as { duration?: number }).duration! || e.startTime);
        }
      }).observe({ type: "largest-contentful-paint", buffered: true });
    } catch {
      /* unsupported */
    }
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          w.__perf.longTasks += 1;
          w.__perf.totalLongTaskMs += Math.max(0, e.duration - 50);
        }
      }).observe({ type: "longtask", buffered: true });
    } catch {
      /* unsupported */
    }
  });
}

/** Read the metrics gathered so far. Call after the screen has settled. */
export async function collectMetrics(page: Page): Promise<PerfMetrics> {
  return page.evaluate(() => {
    const nav = performance.getEntriesByType("navigation")[0] as
      | PerformanceNavigationTiming
      | undefined;
    const paints = performance.getEntriesByType("paint");
    const fcp = paints.find((p) => p.name === "first-contentful-paint")?.startTime ?? null;
    const w = window as unknown as {
      __perf?: { lcp: number; longTasks: number; totalLongTaskMs: number };
    };
    const mem = (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory;
    return {
      ttfb: nav ? nav.responseStart : null,
      fcp,
      lcp: w.__perf && w.__perf.lcp > 0 ? w.__perf.lcp : fcp,
      dcl: nav ? nav.domContentLoadedEventEnd : null,
      load: nav ? nav.loadEventEnd : null,
      longTasks: w.__perf?.longTasks ?? 0,
      totalLongTaskMs: Math.round(w.__perf?.totalLongTaskMs ?? 0),
      jsHeapBytes: mem ? mem.usedJSHeapSize : null,
    };
  });
}

/** Soft budgets (ms) for a dev-server cold load on a developer laptop. Generous
 *  on purpose — CI machines vary wildly; the goal is catching 10x regressions,
 *  not micro-tuning. Override per-env with KINORA_PERF_* envs if needed. */
export const PERF_BUDGET = {
  fcp: Number(process.env.KINORA_PERF_FCP ?? 4000),
  lcp: Number(process.env.KINORA_PERF_LCP ?? 6000),
  totalLongTaskMs: Number(process.env.KINORA_PERF_LONGTASK ?? 2500),
} as const;

/** Persist a metrics JSON next to the test's output dir + the shared artifacts. */
export function writeMetrics(
  testInfo: TestInfo,
  name: string,
  metrics: PerfMetrics,
): void {
  const payload = JSON.stringify(
    { name, capturedAt: new Date().toISOString(), budget: PERF_BUDGET, metrics },
    null,
    2,
  );
  // Attach to the HTML report.
  testInfo.attachments.push({
    name: `perf-${name}.json`,
    contentType: "application/json",
    body: Buffer.from(payload),
  });
  // Also drop into the shared artifacts dir for cross-agent dashboards.
  try {
    const dir = path.resolve(process.cwd(), "../../coordination/artifacts/e2e-perf");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `perf-${name}.json`), payload);
  } catch {
    /* artifacts dir optional */
  }
}
