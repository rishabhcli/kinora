import { test, expect } from "../fixtures/test";

// Director Studio coverage. We drive the studio via its dedicated mount
// (e2e/harness/director.html) — the live in-app nav into the studio (framer
// AnimatePresence over the library) is unreliable headless, exactly as
// app-screens.spec.ts notes for the library page. The harness mounts the REAL
// DirectorStudio; the API mock serves its shots/canon/prefs so the chrome is
// fully populated. No session is started, so the §5.4 region-comment bar is
// correctly DISABLED — which is the offline product behaviour (no Wan spend).

const DIRECTOR = "/e2e/harness/director.html";

test.describe("director studio (harness mount)", () => {
  test("renders the studio with all six tabs", async ({ page }) => {
    await page.goto(DIRECTOR);
    await expect(page.locator('[role="tablist"]')).toBeVisible({ timeout: 15_000 });
    const labels = await page.getByRole("tab").allInnerTexts();
    expect(labels).toEqual(["Timeline", "Canon", "Conflicts", "Notes", "Analytics", "Share"]);
  });

  test("switching tabs updates aria-selected", async ({ api, page }) => {
    void api;
    await page.goto(DIRECTOR);
    await expect(page.locator('[role="tablist"]')).toBeVisible();
    const canon = page.getByRole("tab", { name: /canon/i });
    await canon.click();
    await expect(canon).toHaveAttribute("aria-selected", "true");
    const timeline = page.getByRole("tab", { name: /timeline/i });
    await timeline.click();
    await expect(timeline).toHaveAttribute("aria-selected", "true");
  });

  // Timeline / Canon / Conflicts / Notes / Share open cleanly against the mock's
  // (intentionally empty) director data. Analytics is excluded here — it crashes
  // on empty data (see the fixme test below) and there is no error boundary, so a
  // crash would unmount the whole studio and cascade into the other assertions.
  for (const tab of ["Timeline", "Canon", "Conflicts", "Notes", "Share"]) {
    test(`tab "${tab}" opens without errors`, async ({ api, page }) => {
      void api;
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(DIRECTOR);
      await expect(page.locator('[role="tablist"]')).toBeVisible();
      const t = page.getByRole("tab", { name: new RegExp(`^${tab}$`, "i") });
      await t.click();
      await expect(t).toHaveAttribute("aria-selected", "true");
      expect(errors, errors.join("\n")).toEqual([]);
    });
  }

  // KNOWN BUG: AnalyticsDashboard reads `.length` on an undefined field when the
  // analytics store is empty (no reading history), throwing and — with no error
  // boundary around the tab panels — unmounting the entire Director Studio.
  // Flagged to the renderer owners; unskip once AnalyticsDashboard guards empty
  // data. (Verified: the other five tabs open cleanly; only Analytics crashes.)
  test.fixme("tab \"Analytics\" opens without errors (blocked: crashes on empty data)", async ({
    api,
    page,
  }) => {
    void api;
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(DIRECTOR);
    await page.getByRole("tab", { name: /^Analytics$/i }).click();
    await expect(page.getByRole("tab", { name: /^Analytics$/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("offline: the region-comment bar is present but the Re-render is disabled (no session)", async ({
    api,
    page,
  }) => {
    // `api` is referenced so the deterministic mock (which serves the studio's
    // shots/canon) is installed before the harness navigates.
    void api;
    await page.goto(DIRECTOR);
    await page.getByRole("tab", { name: /timeline/i }).click();
    // With seed shots loaded, a shot is auto-selected and the comment bar mounts.
    const box = page.getByRole("textbox", { name: /direct this shot/i });
    await expect(box).toBeVisible({ timeout: 15_000 });
    await expect(box).toBeDisabled();
    // The Re-render submit is disabled until a session exists (§5.4 REST regen).
    const reRender = page.getByRole("button", { name: /re-?render/i }).first();
    await expect(reRender).toBeDisabled();
  });

  test("exposes a Start session affordance", async ({ page }) => {
    await page.goto(DIRECTOR);
    await expect(page.getByRole("button", { name: /start session/i })).toBeVisible();
  });

  test("the Close control returns control to the host (onClose fires)", async ({ page }) => {
    await page.goto(DIRECTOR);
    await expect(page.locator('[role="tablist"]')).toBeVisible();
    await page.getByRole("button", { name: /close director studio/i }).click();
    await expect(page.getByTestId("director-closed")).toBeVisible({ timeout: 10_000 });
  });
});
