import { test as base, expect, type Page } from "@playwright/test";

import { SEED, shotLabel } from "./seed";

/**
 * The `test` every spec imports. The frontend runs directly against the REAL
 * backend contract — there is no compatibility shim: GET /api/books and
 * /api/books/:id/shots return bare arrays, each shot carries `source_span`, and
 * /api/books/:id/canon returns an `entities` array. (Phase-13's `compat.ts` has
 * been deleted now that those mismatches are fixed in the API itself.)
 */
export const test = base;

export { expect };

// --------------------------------------------------------------------------- //
// Auth
// --------------------------------------------------------------------------- //

const EMAIL = 'input[type="email"]';
const PASSWORD = 'input[type="password"]';
const SUBMIT = 'form button[type="submit"]';

/** Log in via the real /login form and wait for the shelf to render. */
export async function login(
  page: Page,
  email: string = SEED.email,
  password: string = SEED.password,
): Promise<void> {
  await page.goto("/login");
  await page.locator(EMAIL).fill(email);
  await page.locator(PASSWORD).fill(password);
  await page.locator(SUBMIT).click();
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
}

/** Register a fresh account via the real form (register → auto-login → shelf). */
export async function register(
  page: Page,
  email: string,
  password = "e2e-register-pw-123",
): Promise<void> {
  await page.goto("/login");
  await page.getByRole("button", { name: "Register" }).click();
  await page.locator(EMAIL).fill(email);
  await page.locator(PASSWORD).fill(password);
  await page.locator(SUBMIT).click();
}

// --------------------------------------------------------------------------- //
// Navigation
// --------------------------------------------------------------------------- //

export interface OpenedBook {
  bookId: string;
  sessionId: string;
}

/**
 * Log in as the seeded user, open the seeded book from the shelf, and wait for
 * the workspace to render. Returns the book id (from the URL) and the session
 * id (captured from the create-session response) for event publishing.
 */
export async function openSeededBook(page: Page): Promise<OpenedBook> {
  await login(page);

  const sessionResponse = page.waitForResponse(
    (r) => /\/api\/sessions$/.test(r.url()) && r.request().method() === "POST",
  );

  const card = page.locator('a[href^="/book/"]').filter({ hasText: SEED.bookTitle });
  await expect(card).toBeVisible();
  await card.click();

  await page.waitForURL(/\/book\//);
  const bookId = page.url().split("/book/")[1].split(/[?#]/)[0];

  // The left pane renders real page images once page metadata loads.
  await expect(page.getByAltText("Page 1")).toBeVisible({ timeout: 30_000 });

  const resp = await sessionResponse;
  const sessionId = (await resp.json()).session_id as string;
  return { bookId, sessionId };
}

// --------------------------------------------------------------------------- //
// Workspace UI helpers
// --------------------------------------------------------------------------- //

/** The visible (non-preload) video element in the stage. */
export function stageVideo(page: Page) {
  return page.locator("video.object-contain");
}

/** Flip the Viewer/Director segmented control. */
export async function switchMode(page: Page, mode: "Viewer" | "Director"): Promise<void> {
  await page.getByRole("tab", { name: mode }).click();
  await expect(page.getByRole("tab", { name: mode })).toHaveAttribute("aria-selected", "true");
}

/** Open a Director sub-tab (timeline / canon / Agent feed). */
export async function openDirectorTab(
  page: Page,
  name: "timeline" | "canon" | "Agent feed",
): Promise<void> {
  await page.getByRole("button", { name, exact: true }).click();
}

/** Click a shot tile in the Director shot timeline (seeks to that shot). */
export async function clickTimelineShot(page: Page, shotId: string): Promise<void> {
  const tile = page.locator("button").filter({ hasText: shotLabel(shotId) });
  await tile.first().scrollIntoViewIfNeeded();
  await tile.first().click();
}

/** Read the persisted JWT (used to authenticate a direct canon-edit call). */
export async function readToken(page: Page): Promise<string> {
  const token = await page.evaluate(() => window.localStorage.getItem("kinora.jwt"));
  expect(token, "expected a persisted JWT after login").toBeTruthy();
  return token as string;
}
