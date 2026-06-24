import { describe, expect, it } from "vitest";

import { type BookResponse } from "../api/types";
import { patchBooksIngestProgress, parseSseDataBlock } from "./library";

describe("parseSseDataBlock", () => {
  it("parses a JSON data line from an SSE frame", () => {
    const raw = parseSseDataBlock(
      'event: ingest_progress\ndata: {"event":"ingest_progress","book_id":"b1","stage":"analyse","pct":0.42}\n',
    );
    expect(raw).toEqual({
      event: "ingest_progress",
      book_id: "b1",
      stage: "analyse",
      pct: 0.42,
    });
  });

  it("returns null for keepalive comments", () => {
    expect(parseSseDataBlock(": keepalive\n")).toBeNull();
  });
});

describe("patchBooksIngestProgress", () => {
  const books: BookResponse[] = [
    {
      id: "b1",
      title: "One",
      status: "importing",
      stage: "extract",
      progress: 0.1,
    },
    { id: "b2", title: "Two", status: "ready" },
  ];

  it("updates stage, progress, and status for a matching book", () => {
    const next = patchBooksIngestProgress(books, "b1", "canon", 0.55);
    expect(next[0]).toMatchObject({
      id: "b1",
      status: "importing",
      stage: "canon",
      progress: 0.55,
    });
    expect(next[1]).toBe(books[1]);
  });

  it("marks ready when the pipeline finishes", () => {
    const next = patchBooksIngestProgress(books, "b1", "ready", 1);
    expect(next[0]?.status).toBe("ready");
  });

  it("leaves the list unchanged when the book id is unknown", () => {
    expect(patchBooksIngestProgress(books, "missing", "analyse", 0.5)).toBe(books);
  });
});
