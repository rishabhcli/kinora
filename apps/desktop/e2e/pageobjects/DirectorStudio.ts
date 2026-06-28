import { expect, type Locator, type Page } from "@playwright/test";
import { BasePage } from "./BasePage";
import { TEXT } from "../support/selectors";

/**
 * The Director Studio overlay: a tablist (Timeline / Canon / Conflicts / Notes /
 * Analytics / Share), a session status, and the region-comment bar. Per the
 * project's §5.4 decision, a region comment POSTs /sessions/{id}/comment to
 * regenerate a shot (REST, not WS). The comment bar is *disabled* until a
 * session is started, which is the default offline state — so the offline specs
 * assert the disabled affordance, while mocked specs can start a session.
 */
export class DirectorStudio extends BasePage {
  readonly root: Locator;
  readonly closeButton: Locator;
  readonly tabs: Locator;
  readonly startSession: Locator;
  readonly commentBox: Locator;
  readonly reRenderButton: Locator;

  constructor(page: Page) {
    super(page);
    this.root = page.locator('[role="tablist"]');
    this.closeButton = page.getByRole("button", { name: TEXT.closeDirector });
    this.tabs = page.getByRole("tab");
    this.startSession = page.getByRole("button", { name: TEXT.startSession });
    this.commentBox = page.getByRole("textbox", { name: /direct this shot/i });
    this.reRenderButton = page.getByRole("button", { name: TEXT.reRender });
  }

  async waitUntilOpen(): Promise<void> {
    await expect(this.root).toBeVisible({ timeout: 15_000 });
  }

  tab(name: string | RegExp): Locator {
    return this.page.getByRole("tab", { name });
  }

  async selectTab(name: string | RegExp): Promise<void> {
    const t = this.tab(name);
    await t.click();
    await expect(t).toHaveAttribute("aria-selected", "true");
  }

  async close(): Promise<void> {
    await this.closeButton.click();
  }
}
