// Client-side feature flags the renderer reads from localStorage. Set these
// BEFORE navigation via page.addInitScript so the app boots with them applied.

import type { Page } from "@playwright/test";

/**
 * Turn the in-reader AI-film generation toggle ON (localStorage
 * "kinora.reading.generateVideo" = "1"). The reader defaults it OFF so no tokens
 * are spent; flipping it on makes useFilmSession create a session + open the SSE
 * stream — which, under the API mock, is fully hermetic (no real Wan, budget 0).
 */
export async function enableAiFilm(page: Page): Promise<void> {
  await page.addInitScript(() => {
    try {
      localStorage.setItem("kinora.reading.generateVideo", "1");
    } catch {
      /* storage blocked */
    }
  });
}

/** Explicitly force the AI-film toggle OFF (the product default). */
export async function disableAiFilm(page: Page): Promise<void> {
  await page.addInitScript(() => {
    try {
      localStorage.setItem("kinora.reading.generateVideo", "0");
    } catch {
      /* storage blocked */
    }
  });
}
