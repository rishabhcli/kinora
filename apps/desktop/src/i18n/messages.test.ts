import { test, expect } from "vitest";
import {
  LOCALES,
  LOCALE_CODES,
  SOURCE_CATALOG,
  SOURCE_LOCALE,
  loadCatalog,
  peekCatalog,
  isLoaded,
  localeMeta,
} from "./messages.ts";
import { flatten } from "../lib/intl/catalog.ts";

test("LOCALE_CODES includes the seed locales incl. the RTL one", () => {
  expect(LOCALE_CODES).toContain("en");
  expect(LOCALE_CODES).toContain("ja");
  expect(LOCALE_CODES).toContain("ar");
  expect(LOCALE_CODES).toContain("pt-BR");
});

test("source catalog is English and always loaded", () => {
  expect(SOURCE_LOCALE).toBe("en");
  expect(isLoaded("en")).toBe(true);
  expect(peekCatalog("en")).toBe(SOURCE_CATALOG);
  // a known key exists
  expect(flatten(SOURCE_CATALOG)["nav.home"]).toBe("Home");
});

test("localeMeta returns direction + names; ar is rtl", () => {
  expect(localeMeta("ar").dir).toBe("rtl");
  expect(localeMeta("en").dir).toBe("ltr");
  expect(localeMeta("ja").name).toBe("日本語");
});

test("LOCALES rows each carry code/name/englishName/dir", () => {
  for (const row of LOCALES) {
    expect(typeof row.code).toBe("string");
    expect(typeof row.name).toBe("string");
    expect(typeof row.englishName).toBe("string");
    expect(["ltr", "rtl"]).toContain(row.dir);
  }
});

test("loadCatalog lazily fetches a real locale chunk", async () => {
  const es = await loadCatalog("es");
  expect(flatten(es)["nav.home"]).toBe("Inicio");
  // second call is memoised → same object
  expect(await loadCatalog("es")).toBe(es);
  expect(isLoaded("es")).toBe(true);
});

test("loadCatalog rejects an unknown locale", async () => {
  await expect(loadCatalog("zz" as never)).rejects.toThrow(/no catalog chunk/);
});

test("every shipped locale chunk loads and is non-empty", async () => {
  for (const code of LOCALE_CODES) {
    const tree = await loadCatalog(code);
    expect(Object.keys(flatten(tree)).length).toBeGreaterThan(0);
  }
});
