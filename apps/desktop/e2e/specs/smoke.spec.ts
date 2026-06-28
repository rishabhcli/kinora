import { test, expect } from "../fixtures/test";

// The single most important journey, end to end, in one spec: sign in → land on
// the library shelves → open a book into the reading room → see the film pane →
// close → log out. If this passes, the app's spine is intact. Kept deliberately
// small + robust so it's the canary in CI.

test("@smoke full reader journey: login → home → open book → reading room → close → logout", async ({
  login,
  home,
  page,
}) => {
  // 1. Login (offline-safe demo entry).
  await login.open();
  await login.signIn();

  // 2. Home shell with shelves.
  await home.waitUntilReady();
  await expect(home.firstCard()).toBeVisible();

  // 3. Open a book → reading room dialog.
  const room = await home.openBook(/The Frog-King/i);
  await expect(room.dialog).toBeVisible();
  await room.waitUntilReading();

  // 4. Film pane present (Ken-Burns fallback; no live Wan).
  await expect.poll(() => room.videoCount(), { timeout: 15_000 }).toBeGreaterThan(0);

  // 5. Close the room.
  await room.closeWithEscape();

  // 6. Log out → back to login.
  await home.logout();
  await expect(login.signInButton).toBeVisible({ timeout: 15_000 });
});
