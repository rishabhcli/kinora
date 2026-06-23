# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: library.spec.ts >> login → library → open seeded book
- Location: e2e/library.spec.ts:12:5

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
  6  | const API = process.env.VITE_KINORA_API_URL ?? "http://localhost:8000";
  7  | 
  8  | /**
  9  |  * Full user smoke against a seeded backend: login → library → open a ready book.
  10 |  * Requires `seed_e2e.py` (or equivalent) against the API at VITE_KINORA_API_URL.
  11 |  */
  12 | test("login → library → open seeded book", async () => {
  13 |   const app = await electron.launch({
  14 |     args: [MAIN],
  15 |     env: { ...process.env, VITE_KINORA_API_URL: API },
  16 |   });
  17 |   try {
> 18 |     const window = await app.firstWindow();
     |                              ^ TimeoutError: electronApplication.firstWindow: Timeout 30000ms exceeded while waiting for event "window"
  19 |     await expect(window.getByText("Kinora")).toBeVisible();
  20 | 
  21 |     // Explore the demo library (e2e seed credentials).
  22 |     await window.getByRole("button", { name: /explore the demo library/i }).click();
  23 | 
  24 |     // Shelf should show the seeded Frog-King title.
  25 |     await expect(window.getByText("The Frog-King (e2e seed)")).toBeVisible({ timeout: 15_000 });
  26 | 
  27 |     // Open the book from its cover button.
  28 |     await window.getByRole("button", { name: /open the frog-king/i }).click();
  29 | 
  30 |     // Reading room: PDF pane + director chrome.
  31 |     await expect(window.getByText(/page 1/i)).toBeVisible({ timeout: 20_000 });
  32 |   } finally {
  33 |     await app.close();
  34 |   }
  35 | });
  36 | 
```