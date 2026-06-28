// Pure precache-manifest + eviction planning — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { buildManifest, planEviction, priorityScore } from "./manifest.ts";

const seg = (src: string, wordStart: number, estBytes?: number) => ({ src, wordStart, estBytes });

test("manifest prioritises clips nearest the reader, forward-biased", () => {
  const m = buildManifest({
    bookId: "b1",
    segments: [seg("far-ahead", 1000), seg("here", 100), seg("just-behind", 90), seg("just-ahead", 110)],
    pageUrls: [],
    focusWord: 100,
    budgetBytes: Number.MAX_SAFE_INTEGER,
  });
  // "here" (distance 0) first; then just-ahead (10) beats just-behind (10*1.05).
  assert.equal(m.clipUrls[0], "here");
  assert.equal(m.clipUrls[1], "just-ahead");
  assert.equal(m.clipUrls[2], "just-behind");
  assert.equal(m.clipUrls[3], "far-ahead");
});

test("manifest packs greedily into the byte budget and reports drops", () => {
  const m = buildManifest({
    bookId: "b1",
    segments: [seg("a", 100, 1000), seg("b", 110, 1000), seg("c", 120, 1000)],
    pageUrls: [],
    focusWord: 100,
    budgetBytes: 2500, // fits two 1000-byte clips, not three
  });
  assert.equal(m.clipUrls.length, 2);
  assert.equal(m.droppedForBudget.length, 1);
  assert.equal(m.plannedBytes, 2000);
});

test("manifest skips blob: srcs and dedupes", () => {
  const m = buildManifest({
    bookId: "b1",
    segments: [seg("blob:x", 100), seg("a", 100), seg("a", 105)],
    pageUrls: ["p1", "p1", "p2"],
    focusWord: 100,
    budgetBytes: Number.MAX_SAFE_INTEGER,
  });
  assert.deepEqual(m.clipUrls, ["a"]); // blob skipped, dup removed
  assert.deepEqual(m.pageUrls, ["p1", "p2"]);
});

test("priorityScore puts the focus word at 0 and biases forward", () => {
  assert.equal(priorityScore(100, 100), 0);
  assert.ok(priorityScore(90, 100) > priorityScore(110, 100)); // behind costs more
});

test("planEviction is a no-op under budget", () => {
  const plan = planEviction([{ src: "a", bytes: 100, lastAccessTick: 1 }], 1000);
  assert.deepEqual(plan.evict, []);
  assert.equal(plan.freedBytes, 0);
});

test("planEviction removes LRU entries until under budget", () => {
  const plan = planEviction(
    [
      { src: "oldest", bytes: 500, lastAccessTick: 1 },
      { src: "mid", bytes: 500, lastAccessTick: 5 },
      { src: "newest", bytes: 500, lastAccessTick: 9 },
    ],
    1000, // total 1500 → must free ≥500
  );
  assert.deepEqual(plan.evict, ["oldest"]);
  assert.equal(plan.freedBytes, 500);
});
