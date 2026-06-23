import { join } from "node:path";

import { _electron as electron, expect, test } from "@playwright/test";

const MAIN = join(__dirname, "..", "out", "main", "index.js");
const API = process.env.VITE_KINORA_API_URL ?? "http://localhost:8000";

/**
 * Full user smoke against a seeded backend: login → library → open a ready book.
 * Requires `seed_e2e.py` (or equivalent) against the API at VITE_KINORA_API_URL.
 */
test("login → library → open seeded book", async () => {
  const app = await electron.launch({
    args: [MAIN],
    env: { ...process.env, VITE_KINORA_API_URL: API },
  });
  try {
    const window = await app.firstWindow();
    await expect(window.getByText("Kinora")).toBeVisible();

    // Explore the demo library (e2e seed credentials).
    await window.getByRole("button", { name: /explore the demo library/i }).click();

    // Shelf should show the seeded Frog-King title.
    await expect(window.getByText("The Frog-King (e2e seed)")).toBeVisible({ timeout: 15_000 });

    // Open the book from its cover button.
    await window.getByRole("button", { name: /open the frog-king/i }).click();

    // Reading room: PDF pane + director chrome.
    await expect(window.getByText(/page 1/i)).toBeVisible({ timeout: 20_000 });
  } finally {
    await app.close();
  }
});
