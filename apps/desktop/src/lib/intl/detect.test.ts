import { test } from "vitest";
import assert from "node:assert/strict";
import { negotiateLocale, resolveCatalogBase, pickInitialLocale } from "./detect.ts";

const SUPPORTED = ["en", "es", "fr", "de", "zh", "hi", "ja", "ar", "pt-BR"];

test("negotiateLocale: exact match wins", () => {
  assert.equal(
    negotiateLocale({ requested: ["fr"], supported: SUPPORTED, fallback: "en" }),
    "fr",
  );
});

test("negotiateLocale: regional request truncates to base catalog", () => {
  assert.equal(
    negotiateLocale({ requested: ["es-MX"], supported: SUPPORTED, fallback: "en" }),
    "es",
  );
  assert.equal(
    negotiateLocale({ requested: ["zh-CN"], supported: SUPPORTED, fallback: "en" }),
    "zh",
  );
});

test("negotiateLocale: requested base satisfied by a supported regional tag", () => {
  // requested "pt" should be satisfied by supported "pt-BR"
  assert.equal(
    negotiateLocale({ requested: ["pt"], supported: SUPPORTED, fallback: "en" }),
    "pt-BR",
  );
});

test("negotiateLocale: walks the requested list, first resolvable wins", () => {
  assert.equal(
    negotiateLocale({
      requested: ["xx", "yy-ZZ", "de-AT", "fr"],
      supported: SUPPORTED,
      fallback: "en",
    }),
    "de",
  );
});

test("negotiateLocale: falls back when nothing matches", () => {
  assert.equal(
    negotiateLocale({ requested: ["xx", "yy"], supported: SUPPORTED, fallback: "en" }),
    "en",
  );
  assert.equal(
    negotiateLocale({ requested: [], supported: SUPPORTED, fallback: "ja" }),
    "ja",
  );
});

test("negotiateLocale: ignores empty/falsey requested entries", () => {
  assert.equal(
    negotiateLocale({ requested: ["", "fr"], supported: SUPPORTED, fallback: "en" }),
    "fr",
  );
});

test("resolveCatalogBase: regional → base, unknown → fallback", () => {
  assert.equal(resolveCatalogBase("es-419", SUPPORTED, "en"), "es");
  assert.equal(resolveCatalogBase("ar-EG", SUPPORTED, "en"), "ar");
  assert.equal(resolveCatalogBase("xx-YY", SUPPORTED, "en"), "en");
  assert.equal(resolveCatalogBase("pt-PT", SUPPORTED, "en"), "pt-BR");
});

test("pickInitialLocale: a supported stored preference always wins", () => {
  assert.equal(
    pickInitialLocale({
      stored: "ja",
      navigator: ["fr-FR", "en"],
      supported: SUPPORTED,
      fallback: "en",
    }),
    "ja",
  );
});

test("pickInitialLocale: regional stored value resolves to its base", () => {
  assert.equal(
    pickInitialLocale({
      stored: "es-AR",
      navigator: ["fr"],
      supported: SUPPORTED,
      fallback: "en",
    }),
    "es",
  );
});

test("pickInitialLocale: no stored → negotiate navigator list", () => {
  assert.equal(
    pickInitialLocale({
      stored: null,
      navigator: ["de-DE", "en-US"],
      supported: SUPPORTED,
      fallback: "en",
    }),
    "de",
  );
});

test("pickInitialLocale: unsupported stored value falls through to navigator", () => {
  assert.equal(
    pickInitialLocale({
      stored: "xx",
      navigator: ["fr"],
      supported: SUPPORTED,
      fallback: "en",
    }),
    "fr",
  );
});
