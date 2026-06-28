import { test, expect } from "../fixtures/test";
import { TEXT } from "../support/selectors";

// Auth / login coverage. The login screen is deliberately forgiving: it races
// the backend against a 6s timeout and enters "demo mode" regardless, so these
// specs verify both the happy path (mocked 200) and the resilient fallback.

test.describe("auth", () => {
  test("renders the login screen with email + password + sign-in", async ({ login }) => {
    await login.open();
    await expect(login.email).toBeVisible();
    await expect(login.password).toBeVisible();
    await expect(login.signInButton).toBeVisible();
  });

  test("prefills the demo credentials", async ({ login }) => {
    await login.open();
    await expect(login.email).toHaveValue(/@kinora\.local$/);
    await expect(login.password).not.toHaveValue("");
  });

  test("sign in with mocked backend enters the app", async ({ login, home }) => {
    await login.open();
    await login.signIn();
    await home.waitUntilReady();
    await expect(home.libraryNav).toBeVisible();
  });

  test('"Explore the demo library" enters the app', async ({ login, home }) => {
    await login.open();
    await login.exploreDemo();
    await home.waitUntilReady();
    await expect(home.libraryNav).toBeVisible();
  });

  test("toggling to register mode shows the create-account heading", async ({ login, page }) => {
    await login.open();
    await login.switchToRegister();
    await expect(page.getByRole("heading", { name: TEXT.signUp })).toBeVisible();
  });

  // Backend completely unreachable → the screen still enters demo mode.
  test.describe("offline fallback", () => {
    test.use({ apiOptions: { offline: true } });
    test("enters demo mode when the backend is unreachable", async ({ login, home }) => {
      await login.open();
      await login.signIn();
      await home.waitUntilReady();
      await expect(home.firstCard()).toBeVisible();
    });
  });

  // Auth endpoints reject → loginOrRegister falls through, still enters.
  test.describe("auth rejected", () => {
    test.use({ apiOptions: { authFails: true } });
    test("still enters the app when login + register both fail", async ({ login, home }) => {
      await login.open();
      await login.signIn();
      await home.waitUntilReady();
      await expect(home.libraryNav).toBeVisible();
    });
  });
});
