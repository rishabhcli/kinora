import { describe, expect, it } from "vitest";

import { applyIngestProgress, displayBookTitle, importGateMessage, stageLabel } from "./shelf";

describe("displayBookTitle", () => {
  it("strips e2e seed suffix", () => {
    expect(displayBookTitle("the frog-king (e2e seed)")).toBe("The Frog-King");
  });
});

describe("stageLabel", () => {
  it("formats snake_case stages", () => {
    expect(stageLabel({ status: "importing", stage: "shot_plan" })).toBe("Shot plan");
  });
  it("reports failed imports", () => {
    expect(stageLabel({ status: "failed", stage: null })).toBe("Import failed");
  });
});

describe("importGateMessage", () => {
  it("mentions the stage for importing books", () => {
    expect(importGateMessage({ status: "importing", stage: "analyse", title: "Demo" })).toMatch(
      /analyse/i,
    );
  });
});

describe("applyIngestProgress", () => {
  it("updates stage and progress for the matching book", () => {
    const books = [
      { id: "a", title: "A", status: "importing", progress: 0.1, stage: "importing" },
      { id: "b", title: "B", status: "ready", progress: 1, stage: null },
    ];
    const next = applyIngestProgress(books, { book_id: "a", stage: "canon", pct: 0.55 });
    expect(next?.[0]?.stage).toBe("canon");
    expect(next?.[0]?.progress).toBe(0.55);
    expect(next?.[1]?.status).toBe("ready");
  });
});
