import { test, expect } from "../fixtures/test";
import { TEXT } from "../support/selectors";

// Top-nav + profile-menu navigation. Screens swap via client-side React state
// (no URL change), so we assert on visible headings/affordances per screen.

test.describe("navigation", () => {
  test("home shell mounts with nav + book shelves", async ({ app, home }) => {
    await expect(home.libraryNav).toBeVisible();
    await expect(home.firstCard()).toBeVisible();
  });

  test("navigates Home → Library and shows the library heading", async ({ app, home, library }) => {
    await home.navigateTo(TEXT.navLibrary);
    await library.waitUntilReady();
    await expect(library.heading).toBeVisible();
  });

  test("navigates to Library then back Home", async ({ app, home, library }) => {
    await home.navigateTo(TEXT.navLibrary);
    await library.waitUntilReady();
    await home.navigateTo(TEXT.navHome);
    await expect(home.firstCard()).toBeVisible();
  });

  test("opens the profile menu and exposes Log Out", async ({ app, home, page }) => {
    await home.openProfileMenu();
    await expect(page.getByRole("button", { name: TEXT.logOut })).toBeVisible();
  });

  test("logging out returns to the login screen", async ({ app, home, login }) => {
    await home.logout();
    await expect(login.signInButton).toBeVisible({ timeout: 15_000 });
  });
});
