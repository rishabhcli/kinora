// Pure SW protocol: asset classification, cache naming, strategy, guards — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyAsset,
  strategyFor,
  clipCacheName,
  pageCacheName,
  isPageToSw,
  isSwToPage,
} from "./swProtocol.ts";

test("classifyAsset recognises clips, pages, and pass-through", () => {
  assert.equal(classifyAsset("https://oss/kinora/clips/a.mp4"), "clip");
  assert.equal(classifyAsset("http://localhost:9000/generated/film-01.mp4"), "clip");
  assert.equal(classifyAsset("https://api/books/b1/pages/12"), "page");
  assert.equal(classifyAsset("https://api/books/b1/page/3?fmt=json"), "page");
  assert.equal(classifyAsset("https://api/books/b1"), null);
  assert.equal(classifyAsset("https://api/auth/login"), null);
});

test("strategyFor maps kinds to caching policy", () => {
  assert.equal(strategyFor("clip"), "cache-first");
  assert.equal(strategyFor("page"), "stale-while-revalidate");
  assert.equal(strategyFor(null), "network-only");
});

test("cache names are versioned + per-book", () => {
  assert.match(clipCacheName("b1"), /^kinora-clips-v\d+-b1$/);
  assert.match(pageCacheName("b1"), /^kinora-pages-v\d+-b1$/);
  assert.notEqual(clipCacheName("b1"), clipCacheName("b2"));
});

test("message guards narrow page⇄worker messages", () => {
  assert.equal(isPageToSw({ type: "PRECACHE", bookId: "b", clipUrls: [], pageUrls: [] }), true);
  assert.equal(isPageToSw({ type: "EVICT_ALL" }), true);
  assert.equal(isPageToSw({ type: "NONSENSE" }), false);
  assert.equal(isPageToSw(null), false);
  assert.equal(isSwToPage({ type: "STATUS", bookId: "b", clips: 1, pages: 2, bytes: 3 }), true);
  assert.equal(isSwToPage({ type: "PRECACHE" }), false);
});
