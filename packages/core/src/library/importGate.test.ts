import { describe, expect, it } from "vitest";

import { bookIsOpenable, importGateMessage } from "./importGate";
import type { BookResponse } from "../api/types";

describe("importGateMessage", () => {
  it("explains failed imports", () => {
    const msg = importGateMessage({ id: "1", title: "X", status: "failed" });
    expect(msg.title).toBe("Import failed");
    expect(msg.body).toMatch(/upload/i);
  });

  it("shows progress for importing books", () => {
    const msg = importGateMessage({
      id: "1",
      title: "X",
      status: "importing",
      stage: "canon",
      progress: 0.42,
    });
    expect(msg.title).toBe("Still preparing");
    expect(msg.body).toMatch(/42%/);
  });
});

describe("bookIsOpenable", () => {
  it("is true only for ready books", () => {
    const ready: BookResponse = { id: "1", title: "X", status: "ready" };
    expect(bookIsOpenable(ready)).toBe(true);
    expect(bookIsOpenable({ ...ready, status: "importing" })).toBe(false);
    expect(bookIsOpenable(null)).toBe(false);
  });
});
