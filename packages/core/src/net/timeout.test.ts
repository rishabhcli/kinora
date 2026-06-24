import { describe, expect, it } from "vitest";

import { withTimeout } from "./timeout";

describe("withTimeout", () => {
  it("resolves when the promise finishes first", async () => {
    const value = await withTimeout(Promise.resolve(42), 50);
    expect(value).toBe(42);
  });

  it("returns null when the deadline passes first", async () => {
    const value = await withTimeout(new Promise<number>(() => undefined), 20);
    expect(value).toBeNull();
  });
});
