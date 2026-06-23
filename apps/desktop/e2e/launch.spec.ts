import { join } from "node:path";

import { _electron as electron, expect, test } from "@playwright/test";

const MAIN = join(__dirname, "..", "out", "main", "index.js");

/**
 * Smoke: the built Electron app launches and renders the login screen. Extends
 * naturally to the full login -> library -> reading-room flow once the CI job
 * points it at a seeded backend (KINORA via VITE_KINORA_API_URL).
 */
test("launches to the login screen", async () => {
  const app = await electron.launch({ args: [MAIN] });
  try {
    const window = await app.firstWindow();
    await expect(window.getByText("Kinora")).toBeVisible();
    await expect(window.getByText("watch the book")).toBeVisible();
  } finally {
    await app.close();
  }
});
