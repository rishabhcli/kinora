import { test } from "node:test";
import assert from "node:assert/strict";
import { coverPrefetchList } from "../../src/components/auth/coverCache.ts";

test("coverPrefetchList: dedupes, drops empties, preserves first-seen order", () => {
  const books = [
    { coverImage: "a.jpg" },
    { coverImage: "" },
    { coverImage: "b.jpg" },
    { coverImage: "a.jpg" }, // dup
    { coverImage: undefined },
    { coverImage: "c.jpg" },
  ];
  assert.deepEqual(coverPrefetchList(books, 10), ["a.jpg", "b.jpg", "c.jpg"]);
});

test("coverPrefetchList: caps at the limit", () => {
  const books = Array.from({ length: 50 }, (_, i) => ({ coverImage: `cover-${i}.jpg` }));
  const out = coverPrefetchList(books, 8);
  assert.equal(out.length, 8);
  assert.equal(out[0], "cover-0.jpg");
  assert.equal(out[7], "cover-7.jpg");
});

test("coverPrefetchList: empty input → empty list (offline-safe, never throws)", () => {
  assert.deepEqual(coverPrefetchList([], 10), []);
  assert.deepEqual(coverPrefetchList(undefined as never, 10), []);
});
