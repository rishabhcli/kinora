import { test } from "node:test";
import assert from "node:assert/strict";
import { pickBackdropVariant, BACKDROP_VARIANTS } from "../../src/components/auth/backdrop.ts";

test("pickBackdropVariant: deterministic — same seed → same variant", () => {
  assert.deepEqual(pickBackdropVariant(7), pickBackdropVariant(7));
  assert.deepEqual(pickBackdropVariant("kinora"), pickBackdropVariant("kinora"));
});

test("pickBackdropVariant: always returns a real variant from the set", () => {
  for (const seed of [0, 1, 2, 3, 99, -5, 1234567]) {
    const v = pickBackdropVariant(seed);
    assert.ok(BACKDROP_VARIANTS.includes(v), `seed ${seed} → in-set`);
    assert.equal(typeof v.beamAngle, "number");
    assert.equal(typeof v.name, "string");
  }
});

test("pickBackdropVariant: cycles across the set as the seed advances", () => {
  const seen = new Set(
    Array.from({ length: BACKDROP_VARIANTS.length }, (_, i) => pickBackdropVariant(i).name),
  );
  // consecutive seeds 0..n-1 should cover every variant exactly once
  assert.equal(seen.size, BACKDROP_VARIANTS.length);
});

test("pickBackdropVariant: string seeds are hashed stably", () => {
  const a = pickBackdropVariant("the-midnight-library");
  const b = pickBackdropVariant("the-midnight-library");
  assert.equal(a.name, b.name);
});
