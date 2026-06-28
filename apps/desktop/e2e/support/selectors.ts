// Centralised, resilient selector vocabulary for the Kinora desktop renderer.
//
// The renderer is under active churn by several agents, so specs MUST NOT
// hard-code brittle CSS. Prefer (in order): ARIA role + accessible name,
// stable visible text (English — i18next default lang is "en"), then a small
// set of intentionally-stable hooks the app already exposes
// (`.book-cover`, `[data-testid="reading-scroll"]`, `[data-reading-scroll]`,
// `[data-testid="read-aloud-text"]`, `[data-testid="scrub-indicator"]`,
// `[data-warmup]`, `role="dialog"`/`role="tab"`).
//
// Everything here is data, not behaviour: page objects compose these into
// Locators. Keeping the vocabulary in one file means a UI rename is a
// one-line patch, not a sweep across a dozen specs.

import type { Page, Locator } from "@playwright/test";

/** English copy that the renderer renders by default (i18next fallbackLng "en"). */
export const TEXT = {
  // Login
  signIn: /^sign in$/i,
  signUp: /^sign up$/i,
  exploreDemo: /explore the demo library/i,
  emailPlaceholder: /email address/i,
  passwordPlaceholder: /^password$/i,
  // Navigation
  navHome: "Home",
  navLibrary: "Library",
  navWatch: "Watch",
  navFavorites: "Favorites",
  navNotes: "Notes",
  profileMenu: /open profile menu/i,
  logOut: /log ?out/i,
  settings: /settings/i,
  // Library
  myLibrary: /my library/i,
  searchLibrary: /search your library/i,
  sortBooks: /sort books/i,
  libraryView: /library view/i,
  directorMode: /director mode/i,
  uploadBook: /upload a book/i,
  // Reading room
  readingDialog: /^reading /i,
  back: /^back$/i,
  closeReader: /close reader/i,
  readingSettings: /reading settings/i,
  aiFilm: /ai film/i,
  bookmark: /bookmark/i,
  highlight: /highlight/i,
  // Reading controls (the a11y panel — owned by the a11y agent, shared surface)
  readAloud: /read aloud/i,
  textSize: /text size/i,
  lineSpacing: /line spacing/i,
  readAloudSpeed: /read-aloud speed/i,
  voice: /^voice$/i,
  readingMode: /reading mode/i,
  // Director
  closeDirector: /close director studio/i,
  startSession: /start session/i,
  reRender: /re-?render/i,
  // Shortcuts
  cheatSheet: /keyboard shortcuts/i,
} as const;

/** Intentionally-stable DOM hooks the renderer already exposes. */
export const HOOK = {
  bookCover: ".book-cover",
  shelfContainer: ".shelf-container",
  readingScroll: '[data-testid="reading-scroll"], [data-reading-scroll]',
  readAloudText: '[data-testid="read-aloud-text"]',
  activeWord: '[data-testid="read-aloud-text"] [aria-current="true"]',
  scrubIndicator: '[data-testid="scrub-indicator"]',
  warmup: "[data-warmup]",
  appMain: "#kinora-main",
  skipLink: ".skip-link",
  paragraph: "[data-para]",
} as const;

export function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * A book card is a `role="button"` whose accessible name reads
 * "<Title> by <Author>…". Passing nothing returns *all* cards; passing a title
 * anchors to that book; passing a RegExp uses it verbatim.
 */
export function bookCard(page: Page, titleOrPattern?: string | RegExp): Locator {
  if (titleOrPattern === undefined) {
    return page.getByRole("button", { name: /\bby\b/i });
  }
  const name =
    typeof titleOrPattern === "string"
      ? new RegExp(`^${escapeRegExp(titleOrPattern)}\\b`, "i")
      : titleOrPattern;
  return page.getByRole("button", { name });
}
