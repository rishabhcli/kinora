# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: launch.spec.ts >> launches to the login screen
- Location: e2e/launch.spec.ts:12:5

# Error details

```
TimeoutError: electronApplication.firstWindow: Timeout 30000ms exceeded while waiting for event "window"
```

# Test source

```ts
  1  | import { join } from "node:path";
  2  | 
  3  | import { _electron as electron, expect, test } from "@playwright/test";
  4  | 
  5  | const MAIN = join(__dirname, "..", "out", "main", "index.js");
  6  | 
  7  | /**
  8  |  * Smoke: the built Electron app launches and renders the login screen. Extends
  9  |  * naturally to the full login -> library -> reading-room flow once the CI job
  10 |  * points it at a seeded backend (KINORA via VITE_KINORA_API_URL).
  11 |  */
  12 | test("launches to the login screen", async () => {
  13 |   const app = await electron.launch({ args: [MAIN] });
  14 |   try {
> 15 |     const window = await app.firstWindow();
     |                              ^ TimeoutError: electronApplication.firstWindow: Timeout 30000ms exceeded while waiting for event "window"
  16 |     await expect(window.getByText("Kinora")).toBeVisible();
  17 |     await expect(window.getByText("watch the book")).toBeVisible();
  18 |   } finally {
  19 |     await app.close();
  20 |   }
  21 | });
  22 | 
```