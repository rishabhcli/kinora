import { test } from "vitest";
import assert from "node:assert/strict";
import {
  formatNumber,
  formatInteger,
  formatDecimal,
  formatPercent,
  formatCompact,
  formatCurrency,
  formatUnit,
  formatDate,
  formatDateTime,
  formatRelative,
  formatRelativeAuto,
  formatList,
  languageDisplayName,
  regionDisplayName,
} from "./format.ts";

test("formatNumber groups by locale", () => {
  assert.equal(formatNumber(1234567.89, "en-US"), "1,234,567.89");
  // de-DE uses '.' for thousands and ',' for decimals
  assert.equal(formatNumber(1234567.89, "de-DE"), "1.234.567,89");
});

test("formatInteger truncates and drops fraction", () => {
  assert.equal(formatInteger(1234.99, "en-US"), "1,234");
});

test("formatDecimal pins fraction digits", () => {
  assert.equal(formatDecimal(3.1, "en-US", 2), "3.10");
});

test("formatPercent", () => {
  assert.equal(formatPercent(0.42, "en-US"), "42%");
});

test("formatCompact uses K/M notation", () => {
  assert.equal(formatCompact(12000, "en-US"), "12K");
  assert.equal(formatCompact(1_500_000, "en-US"), "1.5M");
});

test("formatCurrency renders the locale's currency layout", () => {
  assert.equal(formatCurrency(1234.5, "en-US", "USD"), "$1,234.50");
  // JPY has no minor unit
  assert.equal(formatCurrency(1234, "ja-JP", "JPY"), "￥1,234");
});

test("formatUnit", () => {
  // "5 sec" in en-US short unit display
  const out = formatUnit(5, "en-US", "second");
  assert.match(out, /5/);
  assert.match(out, /sec/);
});

test("formatUnit tolerates exotic units without throwing", () => {
  const out = formatUnit(3, "en-US", "totally-not-a-unit");
  assert.match(out, /3/);
});

test("formatDate medium style", () => {
  const d = new Date(Date.UTC(2026, 5, 28, 12, 0, 0)); // 2026-06-28
  const out = formatDate(d, "en-US", { dateStyle: "long", timeZone: "UTC" });
  assert.match(out, /June 28, 2026/);
});

test("formatDateTime combines date+time", () => {
  const d = new Date(Date.UTC(2026, 0, 2, 9, 5, 0));
  const out = formatDateTime(d, "en-US", "short", "short");
  assert.match(out, /1\/2\/26/);
});

test("formatRelative explicit", () => {
  assert.equal(formatRelative(-1, "day", "en-US"), "yesterday");
  assert.equal(formatRelative(2, "hour", "en-US"), "in 2 hours");
});

test("formatRelativeAuto picks the right unit from a delta", () => {
  const now = new Date(Date.UTC(2026, 5, 28, 12, 0, 0)).getTime();
  const fiveMinAgo = now - 5 * 60 * 1000;
  assert.equal(formatRelativeAuto(fiveMinAgo, "en-US", now), "5 minutes ago");
  const inThreeDays = now + 3 * 24 * 60 * 60 * 1000;
  assert.equal(formatRelativeAuto(inThreeDays, "en-US", now), "in 3 days");
});

test("formatList conjunction", () => {
  assert.equal(formatList(["a", "b", "c"], "en-US"), "a, b, and c");
});

test("display names resolve through Intl.DisplayNames", () => {
  assert.equal(languageDisplayName("fr", "en"), "French");
  assert.equal(regionDisplayName("US", "en"), "United States");
});

test("unknown locale degrades to en formatting rather than throwing", () => {
  // bogus locale string — wrapper should not throw
  assert.doesNotThrow(() => formatNumber(1000, "!!bad!!"));
});

test("formatNumber handles bigint", () => {
  assert.equal(formatNumber(12345678901234567890n, "en-US"), "12,345,678,901,234,567,890");
});

test("formatRelativeAuto walks the unit ladder", () => {
  const now = new Date(Date.UTC(2026, 0, 15, 12, 0, 0)).getTime();
  assert.equal(formatRelativeAuto(now - 30_000, "en-US", now), "30 seconds ago");
  assert.equal(formatRelativeAuto(now - 90 * 60_000, "en-US", now), "1 hour ago");
  assert.equal(formatRelativeAuto(now + 2 * 7 * 86_400_000, "en-US", now), "in 2 weeks");
  // numeric:"auto" renders "last year" rather than "1 year ago"
  assert.equal(formatRelativeAuto(now - 400 * 86_400_000, "en-US", now), "last year");
});

test("formatList disjunction style", () => {
  assert.equal(formatList(["a", "b", "c"], "en-US", { type: "disjunction" }), "a, b, or c");
});
