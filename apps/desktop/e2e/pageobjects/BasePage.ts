import type { Page } from "@playwright/test";

/** Shared behaviour for every page object: a `page` handle + a navigation helper. */
export abstract class BasePage {
  constructor(protected readonly page: Page) {}

  /** Navigate to a path on the dev server (baseURL from the Playwright config). */
  protected async goto(pathname = "/"): Promise<void> {
    await this.page.goto(pathname);
  }
}
