import { test, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { IntlProvider } from "./IntlProvider.tsx";
import { T } from "./T.tsx";
import { _primeCatalog } from "./messages.ts";

beforeEach(() => {
  localStorage.clear();
});

test("default <b> maps to <strong>", () => {
  _primeCatalog("en", { login: { tagline: "Read <b>now</b>" } } as never);
  render(
    <IntlProvider initialLocale="en">
      <T k="login.tagline" />
    </IntlProvider>,
  );
  expect(document.querySelector("strong")?.textContent).toBe("now");
});

test("custom components override defaults", () => {
  _primeCatalog("en", { login: { tagline: "Accept the <link>terms</link>" } } as never);
  render(
    <IntlProvider initialLocale="en">
      <T
        k="login.tagline"
        components={{ link: (c) => <a href="/tos" data-testid="lnk">{c}</a> }}
      />
    </IntlProvider>,
  );
  const a = screen.getByTestId("lnk");
  expect(a.getAttribute("href")).toBe("/tos");
  expect(a.textContent).toBe("terms");
});

test("nested tags render nested elements", () => {
  _primeCatalog("en", { login: { tagline: "<b>bold and <i>italic</i></b>" } } as never);
  render(
    <IntlProvider initialLocale="en">
      <T k="login.tagline" />
    </IntlProvider>,
  );
  const strong = document.querySelector("strong");
  expect(strong?.querySelector("em")?.textContent).toBe("italic");
  expect(strong?.textContent).toBe("bold and italic");
});

test("unknown tag renders children inline (no broken element)", () => {
  _primeCatalog("en", { login: { tagline: "plain <unknown>text</unknown> here" } } as never);
  render(
    <IntlProvider initialLocale="en">
      <span data-testid="wrap">
        <T k="login.tagline" />
      </span>
    </IntlProvider>,
  );
  expect(screen.getByTestId("wrap").textContent).toBe("plain text here");
  expect(document.querySelector("unknown")).toBeNull();
});

test("interpolates args inside the tag", () => {
  _primeCatalog("en", { login: { tagline: "Watch <b>{title}</b>" } } as never);
  render(
    <IntlProvider initialLocale="en">
      <T k="login.tagline" args={{ title: "Dune" }} />
    </IntlProvider>,
  );
  expect(document.querySelector("strong")?.textContent).toBe("Dune");
});
