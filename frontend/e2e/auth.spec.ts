import { expect, login, register, test } from "./fixtures/app";
import { SEED } from "./fixtures/seed";

// §5.1 — auth: register + login both land on the shelf.
test.describe("auth", () => {
  test("registering a new account lands on the shelf", async ({ page }) => {
    const email = `pw-${Date.now()}@kinora.test`;
    await register(page, email);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  });

  test("logging in as the seeded user lands on the shelf", async ({ page }) => {
    await login(page, SEED.email, SEED.password);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  });

  test("a wrong password surfaces an error and stays on /login", async ({ page }) => {
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(SEED.email);
    await page.locator('input[type="password"]').fill("definitely-wrong-password");
    await page.locator('form button[type="submit"]').click();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });
});
