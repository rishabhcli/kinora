import { expect, type Locator, type Page } from "@playwright/test";
import { BasePage } from "./BasePage";
import { TEXT, HOOK } from "../support/selectors";

/**
 * The vertical reading room. Modeled as a `role="dialog"` whose accessible name
 * is "Reading <Title>". With KINORA_LIVE_VIDEO off (and the client AI-film
 * toggle off), the film pane plays bundled Ken-Burns fallback mp4s — no live
 * generation. This page object covers open/close, the scroll-film engine, the
 * settings popover, and the AI-film / bookmark / highlight toggles.
 */
export class ReadingRoom extends BasePage {
  readonly dialog: Locator;
  readonly backButton: Locator;
  readonly scrollContainer: Locator;
  readonly settingsButton: Locator;
  readonly aiFilmToggle: Locator;
  readonly bookmarkToggle: Locator;
  readonly highlightToggle: Locator;
  readonly warmup: Locator;
  readonly scrubIndicator: Locator;

  constructor(page: Page) {
    super(page);
    this.dialog = page.getByRole("dialog", { name: TEXT.readingDialog });
    // The back control's accessible name is its aria-label ("Close reader and go
    // back"), which overrides the visible "Back" text — match the aria-label.
    this.backButton = page.getByRole("button", { name: TEXT.closeReader });
    this.scrollContainer = page.locator(HOOK.readingScroll);
    this.settingsButton = page.getByRole("button", { name: TEXT.readingSettings });
    this.aiFilmToggle = page.locator('button[aria-label*="AI film" i]');
    this.bookmarkToggle = page.getByRole("button", { name: TEXT.bookmark });
    this.highlightToggle = page.getByRole("button", { name: TEXT.highlight });
    this.warmup = page.locator(HOOK.warmup);
    this.scrubIndicator = page.locator(HOOK.scrubIndicator);
  }

  async waitUntilOpen(): Promise<void> {
    await expect(this.dialog).toBeVisible({ timeout: 20_000 });
  }

  /** Wait for the reading text to be present (warm-up finished into "reading"). */
  async waitUntilReading(): Promise<void> {
    await expect(this.scrollContainer.first()).toBeVisible({ timeout: 20_000 });
  }

  async title(): Promise<string> {
    return (await this.dialog.getAttribute("aria-label")) ?? "";
  }

  async closeWithEscape(): Promise<void> {
    await this.page.keyboard.press("Escape");
    await expect(this.dialog).toHaveCount(0, { timeout: 10_000 });
  }

  async closeWithBackButton(): Promise<void> {
    await this.backButton.first().click();
    await expect(this.dialog).toHaveCount(0, { timeout: 10_000 });
  }

  /** Scroll the reading text to a 0..1 fraction; this scrubs the film pane. */
  async scrollToFraction(fraction: number): Promise<void> {
    await this.scrollContainer.first().evaluate((el, f) => {
      const max = el.scrollHeight - el.clientHeight;
      el.scrollTop = Math.max(0, Math.min(max, max * (f as number)));
      el.dispatchEvent(new Event("scroll"));
    }, fraction);
  }

  async openSettings(): Promise<void> {
    await this.settingsButton.first().click();
  }

  async toggleAiFilm(): Promise<void> {
    await this.aiFilmToggle.first().click();
  }

  async aiFilmPressed(): Promise<boolean> {
    return (await this.aiFilmToggle.first().getAttribute("aria-pressed")) === "true";
  }

  videoCount(): Promise<number> {
    return this.page.locator("video").count();
  }
}
