import { test, expect } from "../fixtures/test";
import { collectMetrics, writeMetrics, PERF_BUDGET } from "../support/perf";
import { settle } from "../support/stabilize";

// Performance tracing: capture FCP / LCP / long-task jank from the browser's own
// Performance APIs on the cold login load and on the home shell, write the
// numbers as CI artifacts, and assert SOFT budgets (see support/perf.ts). The
// budgets are intentionally generous — the goal is catching order-of-magnitude
// regressions (a bundle bloat, a blocking sync layout), not micro-tuning. These
// run UNFROZEN so the measured paint reflects the real animated startup.

test.describe("performance", () => {
  test.use({ frozen: false });

  test("login cold-load paint + jank within budget", async ({ login, page }, testInfo) => {
    await login.open();
    await settle(page);
    const metrics = await collectMetrics(page);
    writeMetrics(testInfo, "login", metrics);

    expect.soft(metrics.fcp, `FCP ${metrics.fcp}ms`).toBeLessThan(PERF_BUDGET.fcp);
    if (metrics.lcp != null) {
      expect.soft(metrics.lcp, `LCP ${metrics.lcp}ms`).toBeLessThan(PERF_BUDGET.lcp);
    }
    expect
      .soft(metrics.totalLongTaskMs, `long-task ${metrics.totalLongTaskMs}ms`)
      .toBeLessThan(PERF_BUDGET.totalLongTaskMs);
  });

  test("home shell paint + jank within budget", async ({ app, page }, testInfo) => {
    await settle(page);
    const metrics = await collectMetrics(page);
    writeMetrics(testInfo, "home", metrics);

    expect.soft(metrics.fcp, `FCP ${metrics.fcp}ms`).toBeLessThan(PERF_BUDGET.fcp);
    expect
      .soft(metrics.totalLongTaskMs, `long-task ${metrics.totalLongTaskMs}ms`)
      .toBeLessThan(PERF_BUDGET.totalLongTaskMs);
  });

  test("metrics are captured (sanity: FCP is a positive number)", async ({ login, page }) => {
    await login.open();
    await settle(page);
    const metrics = await collectMetrics(page);
    expect(metrics.fcp).not.toBeNull();
    expect(metrics.fcp!).toBeGreaterThan(0);
  });
});
