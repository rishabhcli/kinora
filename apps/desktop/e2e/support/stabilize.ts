// Helpers that make a churny, animated, media-heavy renderer deterministic
// enough to screenshot and assert against.
//
// Kinora leans hard on framer-motion, ambient canvas animations, time-based
// greetings, video crossfades, and randomised cover gradients. For visual
// regression and stable assertions we freeze the non-essential motion and
// neutralise time/random so two runs produce identical pixels.

import type { Page } from "@playwright/test";

/**
 * Inject CSS + JS that disables animations/transitions, pins time-of-day, and
 * stops video autoplay churn. Call BEFORE navigation (it uses addInitScript +
 * addStyleTag-on-load). Idempotent.
 */
export async function freezeMotion(page: Page): Promise<void> {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(() => {
    // Pin the clock so the time-based greeting ("Good evening, …") is stable.
    const FIXED = new Date("2026-01-01T20:00:00Z").getTime();
    const RealDate = Date;
    // Keep Date construction working, but make `new Date()` / Date.now() fixed.
    class FrozenDate extends RealDate {
      constructor(...args: unknown[]) {
        if (args.length === 0) super(FIXED);
        else super(...(args as ConstructorParameters<typeof RealDate>));
      }
      static now() {
        return FIXED;
      }
    }
    (window as unknown as { Date: unknown }).Date = FrozenDate;

    // Make Math.random deterministic (cover gradients, ambient particles).
    let seed = 0x2545f491;
    Math.random = () => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed / 0x7fffffff;
    };
  });

  // Kill animations/transitions + freeze videos once the document is up.
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation-duration: 0s !important;
        animation-delay: 0s !important;
        transition-duration: 0s !important;
        transition-delay: 0s !important;
        scroll-behavior: auto !important;
      }
      .auth-enter-flash { display: none !important; }
      video { visibility: hidden !important; }
    `,
  }).catch(() => {
    /* page may not be navigated yet — freezeForSnapshot reapplies post-nav */
  });
}

/**
 * Re-apply the freeze stylesheet after navigation/route change (addStyleTag is
 * per-document). Also pauses any playing <video> so the film pane is a still.
 */
export async function freezeForSnapshot(page: Page): Promise<void> {
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        transition: none !important;
      }
      video { visibility: hidden !important; }
    `,
  });
  await page.evaluate(() => {
    document.querySelectorAll("video").forEach((v) => {
      try {
        v.pause();
        v.currentTime = 0;
      } catch {
        /* ignore */
      }
    });
  });
}

/** Wait until the network is idle AND fonts are ready (reduces text reflow). */
export async function settle(page: Page, timeout = 10_000): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout }).catch(() => {});
  await page
    .evaluate(() => (document as unknown as { fonts?: { ready: Promise<unknown> } }).fonts?.ready)
    .catch(() => {});
}
