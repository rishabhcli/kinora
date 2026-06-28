import { expect, type Locator, type Page } from "@playwright/test";
import { BasePage } from "./BasePage";
import { TEXT } from "../support/selectors";

/**
 * The auth screen. Critically, LoginPage.enter() resolves whether or not the
 * backend answers: the real component races login against a 6s timeout and calls
 * onEnter() regardless ("continue in demo mode"). So `signIn()` works both with
 * the API mock and fully offline.
 */
export class LoginScreen extends BasePage {
  readonly email: Locator;
  readonly password: Locator;
  readonly signInButton: Locator;
  readonly exploreDemoLink: Locator;

  constructor(page: Page) {
    super(page);
    this.email = page.getByPlaceholder(TEXT.emailPlaceholder);
    this.password = page.getByPlaceholder(TEXT.passwordPlaceholder);
    this.signInButton = page.getByRole("button", { name: TEXT.signIn });
    this.exploreDemoLink = page.getByRole("button", { name: TEXT.exploreDemo });
  }

  async open(): Promise<void> {
    await this.goto("/");
    await expect(this.signInButton).toBeVisible({ timeout: 15_000 });
  }

  async fillCredentials(email: string, password: string): Promise<void> {
    await this.email.fill(email);
    await this.password.fill(password);
  }

  /** Submit the login form (the form's own demo email/password are prefilled). */
  async signIn(): Promise<void> {
    await this.signInButton.click();
  }

  /** Enter via the "Explore the demo library" affordance. */
  async exploreDemo(): Promise<void> {
    await this.exploreDemoLink.click();
  }

  /** Toggle between login and register modes via the footer link. */
  async switchToRegister(): Promise<void> {
    await this.page.getByRole("button", { name: TEXT.signUp }).last().click();
  }
}
