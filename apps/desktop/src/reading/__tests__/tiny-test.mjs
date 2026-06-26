// A dependency-free test harness for Agent-02's pure logic.
//
// There is no vitest/jest in @kinora/desktop (only `tsc` + `vite build`), and we
// must not add a runner to the shared package config. Node >= 22.6 strips TS
// types natively, so we run `node --experimental-strip-types foo.test.ts` and use
// this harness for assertions. Lives as `.mjs` so `tsc` (which only compiles
// `src/**/*.ts[x]`) never sees it.
//
// Register-then-run: `test()` queues a case, and the test file ends with
// `await done()`, which runs every case (awaiting async ones), prints the
// summary, and sets a non-zero exit code on any failure.
import assert from "node:assert/strict";

const queue = [];

/** Queue one test case. Sync or async `fn`. */
export function test(name, fn) {
  queue.push({ name, fn });
}

// Only forward `msg` when given — passing an explicit `undefined` makes node's
// assert throw a confusing TypeError instead of its clean value diff.
export const eq = (actual, expected, msg) =>
  msg === undefined
    ? assert.deepStrictEqual(actual, expected)
    : assert.deepStrictEqual(actual, expected, msg);
export const ok = (value, msg) =>
  msg === undefined ? assert.ok(value) : assert.ok(value, msg);
/** Assert a number is within `tol` of `expected` (for float playhead math). */
export const close = (actual, expected, tol = 1e-9, msg) =>
  assert.ok(
    Math.abs(actual - expected) <= tol,
    msg ?? `expected ${actual} to be within ${tol} of ${expected}`,
  );

/** Run every queued case, print the summary, set exit code on failure. */
export async function done() {
  let passed = 0;
  let failed = 0;
  const failures = [];
  for (const { name, fn } of queue) {
    try {
      await fn();
      passed += 1;
      process.stdout.write(`  ✓ ${name}\n`);
    } catch (err) {
      failed += 1;
      failures.push({ name, err });
      process.stdout.write(`  ✗ ${name}\n`);
    }
  }
  process.stdout.write(`\n${passed} passed, ${failed} failed\n`);
  for (const { name, err } of failures) {
    process.stdout.write(`\nFAILED: ${name}\n${err?.stack ?? err}\n`);
  }
  if (failed > 0) process.exitCode = 1;
}
