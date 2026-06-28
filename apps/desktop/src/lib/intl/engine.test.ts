import { test } from "vitest";
import assert from "node:assert/strict";
import { Translator, createTranslator } from "./engine.ts";

const EN = {
  nav: { home: "Home" },
  greet: "Hi {name}",
  items: "{n, plural, one {# item} other {# items}}",
  rich: "Read <b>{title}</b>",
};
const ES = {
  nav: { home: "Inicio" },
  greet: "Hola {name}",
  // intentionally missing `items` + `rich` → should fall back to en
};

function tr(locale = "en") {
  return createTranslator({ locale, fallback: "en" }, { en: EN, es: ES });
}

test("t resolves a key in the active locale", () => {
  const t = tr("es");
  assert.equal(t.t("nav.home"), "Inicio");
  assert.equal(t.t("greet", { name: "Ana" }), "Hola Ana");
});

test("t falls back to the source locale for an untranslated key", () => {
  const t = tr("es");
  assert.equal(t.t("items", { n: 3 }), "3 items"); // from en
});

test("missing key returns the key (dev) or empty", () => {
  const t = tr("en");
  assert.equal(t.t("does.not.exist"), "does.not.exist");
  const t2 = createTranslator({ locale: "en", onMissing: "empty" }, { en: EN });
  assert.equal(t2.t("nope"), "");
});

test("onMissingKey callback fires for unresolved keys", () => {
  const seen: string[] = [];
  const t = createTranslator(
    { locale: "en", onMissingKey: (k) => seen.push(k) },
    { en: EN },
  );
  t.t("ghost.key");
  assert.deepEqual(seen, ["ghost.key"]);
});

test("setLocale switches the active locale + resolves regional to base", () => {
  const t = tr("en");
  t.setLocale("es-MX"); // no es-MX catalog → resolves to es
  assert.equal(t.getLocale(), "es");
  assert.equal(t.t("nav.home"), "Inicio");
});

test("subscribe is notified on locale change", () => {
  const t = tr("en");
  let notified = "";
  const off = t.subscribe((l) => (notified = l));
  t.setLocale("es");
  assert.equal(notified, "es");
  off();
  t.setLocale("en");
  assert.equal(notified, "es"); // unsubscribed
});

test("pseudo mode wraps + accents but keeps placeholders", () => {
  const t = tr("en");
  t.setPseudo(true);
  const out = t.t("greet", { name: "Ada" });
  assert.ok(out.startsWith("⟦"));
  assert.ok(out.includes("Ada")); // the arg value is interpolated as-is
});

test("direction reflects the locale", () => {
  const t = createTranslator({ locale: "ar", fallback: "en" }, { en: EN, ar: EN });
  assert.equal(t.direction(), "rtl");
  t.setLocale("en");
  assert.equal(t.direction(), "ltr");
});

test("tParts preserves rich-text tags, falling back to en", () => {
  const t = tr("es");
  const parts = t.tParts("rich", { title: "Dune" });
  assert.equal(parts[1].type, "tag");
  if (parts[1].type === "tag") assert.equal(parts[1].name, "b");
});

test("exists checks the fallback chain", () => {
  const t = tr("es");
  assert.equal(t.exists("items"), true); // via en fallback
  assert.equal(t.exists("totally.absent"), false);
});

test("convenience formatters bind to the active locale", () => {
  const t = tr("en");
  t.setLocale("de");
  // de catalog absent → resolves to fallback en, but the *locale string* used for
  // formatting is whatever resolveCatalogBase returns; register de to be explicit.
  const t2 = createTranslator({ locale: "de", fallback: "en" }, { en: EN, de: EN });
  assert.equal(t2.number(1234.5), "1.234,5");
  // Normalise the (version-dependent) space before the currency symbol.
  assert.equal(t2.currency(5, "EUR").replace(/\s/g, " "), "5,00 €");
  assert.equal(t2.list(["a", "b"]), "a und b");
});

test("Translator can be built incrementally", () => {
  const t = new Translator({ locale: "en" });
  t.register("en", EN);
  assert.equal(t.t("nav.home"), "Home");
  assert.equal(t.has("en"), true);
  assert.deepEqual(t.registeredLocales(), ["en"]);
});
