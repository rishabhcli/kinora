import { describe, expect, it } from "vitest";

import type { BookResponse } from "../api/types";
import {
  applyIngestProgress,
  isTerminalIngest,
  parseIngestProgress,
  statusFromIngestStage,
} from "./ingest";

const sample: BookResponse = {
  id: "book_1",
  title: "Demo",
  status: "importing",
  progress: 0.1,
  stage: "importing",
};

describe("parseIngestProgress", () => {
  it("parses a live ingest_progress SSE payload", () => {
    const event = parseIngestProgress({
      event: "ingest_progress",
      book_id: "book_1",
      stage: "analyze",
      pct: 0.45,
    });
    expect(event).toEqual({
      event: "ingest_progress",
      book_id: "book_1",
      stage: "analyze",
      pct: 0.45,
    });
  });

  it("returns null for unrelated events", () => {
    expect(parseIngestProgress({ event: "clip_ready", shot_id: "s1" })).toBeNull();
  });
});

describe("applyIngestProgress", () => {
  it("patches the matching book in the shelf cache", () => {
    const next = applyIngestProgress([sample], {
      event: "ingest_progress",
      book_id: "book_1",
      stage: "canon",
      pct: 0.6,
    });
    expect(next?.[0]).toMatchObject({ stage: "canon", progress: 0.6, status: "importing" });
  });

  it("marks ready when the ingest stage completes", () => {
    const next = applyIngestProgress([sample], {
      event: "ingest_progress",
      book_id: "book_1",
      stage: "ready",
      pct: 1,
    });
    expect(next?.[0]?.status).toBe("ready");
    expect(isTerminalIngest({ event: "ingest_progress", book_id: "book_1", stage: "ready", pct: 1 })).toBe(true);
  });

  it("maps failed ingest to a failed shelf status", () => {
    expect(statusFromIngestStage("failed")).toBe("failed");
  });
});
