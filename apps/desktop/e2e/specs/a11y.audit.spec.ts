import { test, expect } from "../fixtures/test";
import { audit, seriousOnly, writeReport, summarize } from "../support/axe";

// Per-screen axe-core audits across the REAL app flow (login → home → reading
// room) + the director harness. This COMPLEMENTS the existing e2e/a11y.spec.ts
// (which scans the owned a11y surfaces): here we walk the full screens, write a
// findings report for each as a CI artifact, and assert a soft gate — ZERO
// serious/critical on screens whose chrome is broadly owned, while still
// recording every finding for triage.
//
// We do NOT fail on moderate/minor (those are tracked via the JSON artifacts) so
// this audit stays useful as the UI churns rather than becoming a blanket
// red-X — but a NEW serious/critical regression breaks the build.

test.describe("accessibility audits", () => {
  test("login screen: report + no serious/critical", async ({ login, page }, testInfo) => {
    await login.open();
    const report = await audit(page, "login");
    writeReport(testInfo, report);
    const serious = seriousOnly(report);
    expect(serious, summarize(serious)).toEqual([]);
  });

  test("home shell: report + no serious/critical", async ({ app, page }, testInfo) => {
    const report = await audit(page, "home");
    writeReport(testInfo, report);
    const serious = seriousOnly(report);
    expect(serious, summarize(serious)).toEqual([]);
  });

  test("reading room: report + no serious/critical", async ({ app, home, page }, testInfo) => {
    const room = await home.openBook();
    await room.waitUntilReading();
    await page.waitForTimeout(500);
    const report = await audit(page, "reading-room");
    writeReport(testInfo, report);
    const serious = seriousOnly(report);
    expect(serious, summarize(serious)).toEqual([]);
  });

  test("director studio (harness): report + no serious/critical", async ({ page }, testInfo) => {
    await page.goto("/e2e/harness/director.html");
    await expect(page.locator('[role="tablist"]')).toBeVisible({ timeout: 15_000 });
    const report = await audit(page, "director");
    writeReport(testInfo, report);
    const serious = seriousOnly(report);
    expect(serious, summarize(serious)).toEqual([]);
  });
});
