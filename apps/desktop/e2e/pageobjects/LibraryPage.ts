import { expect, type Locator, type Page } from "@playwright/test";
import { BasePage } from "./BasePage";
import { TEXT, bookCard } from "../support/selectors";

/**
 * The library screen (reached via the "Library" nav from Home, or mounted
 * standalone in e2e/harness/library.html). Heading "My Library", a search box,
 * a sort select, genre chips, and the upload affordance.
 */
export class LibraryPage extends BasePage {
  readonly heading: Locator;
  readonly search: Locator;
  readonly sort: Locator;
  readonly uploadZone: Locator;

  constructor(page: Page) {
    super(page);
    this.heading = page.getByRole("heading", { name: TEXT.myLibrary });
    this.search = page.getByRole("searchbox", { name: TEXT.searchLibrary });
    this.sort = page.getByRole("combobox", { name: TEXT.sortBooks });
    this.uploadZone = page.getByRole("button", { name: TEXT.uploadBook });
  }

  async waitUntilReady(): Promise<void> {
    await expect(this.heading).toBeVisible({ timeout: 20_000 });
  }

  /** Open the library page standalone via the E2E harness (no nav choreography). */
  async openHarness(): Promise<void> {
    await this.goto("/e2e/harness/library.e2e.html");
    await this.waitUntilReady();
  }

  async searchFor(query: string): Promise<void> {
    await this.search.fill(query);
  }

  card(title: string | RegExp): Locator {
    return bookCard(this.page, title).first();
  }

  cards(): Locator {
    return bookCard(this.page);
  }

  genreChip(name: string | RegExp): Locator {
    return this.page.getByRole("button", { name });
  }
}
