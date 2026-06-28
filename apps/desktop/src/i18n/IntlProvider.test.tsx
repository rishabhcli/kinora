import { test, expect, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import { IntlProvider, LANG_STORAGE_KEY } from "./IntlProvider.tsx";
import { useT, useLocale, useDirection, useLocaleSwitch, useFormatters } from "./useT.ts";
import { T } from "./T.tsx";
import { _primeCatalog } from "./messages.ts";

// Prime a couple of catalogs synchronously so switching doesn't need a real chunk.
beforeEach(() => {
  localStorage.clear();
  _primeCatalog("es", {
    nav: { home: "Inicio" },
    common: { save: "Guardar" },
  } as never);
  _primeCatalog("ar", { nav: { home: "الرئيسية" } } as never);
});

afterEach(() => {
  document.documentElement.lang = "en";
  document.documentElement.dir = "ltr";
});

function Probe() {
  const t = useT();
  const locale = useLocale();
  const dir = useDirection();
  const { setLocale } = useLocaleSwitch();
  return (
    <div>
      <span data-testid="home">{t("nav.home")}</span>
      <span data-testid="locale">{locale}</span>
      <span data-testid="dir">{dir}</span>
      <button onClick={() => void setLocale("es")}>es</button>
      <button onClick={() => void setLocale("ar")}>ar</button>
    </div>
  );
}

test("renders the source locale by default", () => {
  render(
    <IntlProvider initialLocale="en">
      <Probe />
    </IntlProvider>,
  );
  expect(screen.getByTestId("home").textContent).toBe("Home");
  expect(screen.getByTestId("locale").textContent).toBe("en");
  expect(screen.getByTestId("dir").textContent).toBe("ltr");
});

test("switching to a primed locale updates strings + persists", async () => {
  render(
    <IntlProvider initialLocale="en">
      <Probe />
    </IntlProvider>,
  );
  await act(async () => {
    screen.getByText("es").click();
  });
  await waitFor(() => expect(screen.getByTestId("home").textContent).toBe("Inicio"));
  expect(screen.getByTestId("locale").textContent).toBe("es");
  await waitFor(() => expect(localStorage.getItem(LANG_STORAGE_KEY)).toBe("es"));
});

test("switching to an RTL locale flips direction + <html dir>", async () => {
  render(
    <IntlProvider initialLocale="en">
      <Probe />
    </IntlProvider>,
  );
  await act(async () => {
    screen.getByText("ar").click();
  });
  await waitFor(() => expect(screen.getByTestId("dir").textContent).toBe("rtl"));
  expect(document.documentElement.dir).toBe("rtl");
  expect(document.documentElement.lang).toBe("ar");
});

test("falls back to the source locale for an untranslated key", () => {
  // es is primed without "common.close"; en has it → fallback chain resolves it.
  function CloseProbe() {
    const t = useT();
    return <span data-testid="close">{t("common.close")}</span>;
  }
  render(
    <IntlProvider initialLocale="es">
      <CloseProbe />
    </IntlProvider>,
  );
  expect(screen.getByTestId("close").textContent).toBe("Close");
});

test("<T> renders rich-text tags as elements", () => {
  // Prime es with a rich message and a key that exists in the en source too.
  _primeCatalog("es", { nav: { home: "Inicio" }, login: { tagline: "Lee <b>{title}</b>" } } as never);
  render(
    <IntlProvider initialLocale="es">
      <T k="login.tagline" args={{ title: "Dune" }} />
    </IntlProvider>,
  );
  const strong = document.querySelector("strong");
  expect(strong?.textContent).toBe("Dune");
});

test("useFormatters binds to the active locale", () => {
  function FmtProbe() {
    const { number } = useFormatters();
    return <span data-testid="n">{number(1234.5)}</span>;
  }
  render(
    <IntlProvider initialLocale="en">
      <FmtProbe />
    </IntlProvider>,
  );
  expect(screen.getByTestId("n").textContent).toBe("1,234.5");
});

test("useT throws outside a provider", () => {
  function Bare() {
    useT();
    return null;
  }
  // Suppress React's error boundary console noise for this expected throw.
  expect(() => render(<Bare />)).toThrow(/within an <IntlProvider>/);
});
