import { test, expect } from "../fixtures/test";

// Library coverage. Uses the standalone harness mount (e2e/harness/library.html)
// for the deterministic LibraryPage — the live in-app nav swap (framer
// AnimatePresence crossfade) is unreliable headless, a fact the existing
// app-screens.spec.ts also documents. The in-app reachability is covered by
// navigation.spec.ts.

test.describe("library (harness mount)", () => {
  test("renders the library heading + search + sort", async ({ library }) => {
    await library.openHarness();
    await expect(library.heading).toBeVisible();
    await expect(library.search).toBeVisible();
    await expect(library.sort).toBeVisible();
  });

  test("shows the upload affordance", async ({ library, upload }) => {
    await library.openHarness();
    await upload.expectAffordanceVisible();
  });

  test("search box accepts input", async ({ library }) => {
    await library.openHarness();
    await library.searchFor("frog");
    await expect(library.search).toHaveValue("frog");
  });

  test("renders at least one book card (offline fallback shelves)", async ({ library }) => {
    await library.openHarness();
    await expect(library.cards().first()).toBeVisible({ timeout: 15_000 });
  });
});

// In-app library: reach it through the nav with the mocked backend serving the
// seed library, and assert a seed book shows up.
test.describe("library (in-app, mocked backend)", () => {
  test("the seed public-domain book is reachable from Home", async ({ app, home }) => {
    await expect(home.card(/The Frog-King/i)).toBeVisible({ timeout: 15_000 });
  });
});
