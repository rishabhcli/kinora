import { expect, type Locator, type Page } from "@playwright/test";
import { BasePage } from "./BasePage";
import { TEXT, bookCard } from "../support/selectors";
import { ReadingRoom } from "./ReadingRoom";

/**
 * The home/shell surface: top nav, profile menu, book shelves. Navigation is
 * client-side React state (no URL change), so we observe DOM visibility, not
 * routing. The home page is React.lazy — `waitUntilReady()` waits for the first
 * nav button so specs don't race the chunk load.
 */
export class HomePage extends BasePage {
  readonly libraryNav: Locator;
  readonly homeNav: Locator;
  readonly profileMenuButton: Locator;

  constructor(page: Page) {
    super(page);
    this.libraryNav = page.getByRole("button", { name: TEXT.navLibrary }).first();
    this.homeNav = page.getByRole("button", { name: TEXT.navHome }).first();
    this.profileMenuButton = page.getByRole("button", { name: TEXT.profileMenu });
  }

  /** Resolve once the home shell has mounted (nav + at least one book card). */
  async waitUntilReady(): Promise<void> {
    await expect(this.libraryNav).toBeVisible({ timeout: 20_000 });
    await expect(this.firstCard()).toBeVisible({ timeout: 20_000 });
  }

  firstCard(): Locator {
    return bookCard(this.page).first();
  }

  card(title: string | RegExp): Locator {
    return bookCard(this.page, title).first();
  }

  /** Switch screens via the top nav (Home/Library/Watch/Favorites/Notes). */
  async navigateTo(label: string): Promise<void> {
    await this.page.getByRole("button", { name: label }).first().click();
  }

  async openProfileMenu(): Promise<void> {
    await this.profileMenuButton.click();
  }

  async logout(): Promise<void> {
    await this.openProfileMenu();
    await this.page.getByRole("button", { name: TEXT.logOut }).click();
  }

  /** Open a book into the reading room. Returns the reading-room page object. */
  async openBook(title?: string | RegExp): Promise<ReadingRoom> {
    const card = title ? this.card(title) : this.firstCard();
    await card.scrollIntoViewIfNeeded();
    await card.click();
    const room = new ReadingRoom(this.page);
    await room.waitUntilOpen();
    return room;
  }
}
