// Pure fallback-path logic: which bundled film + which shot + which src to paint
// so the reader NEVER sees a black void, with or without a live backend.
import test from "node:test";
import assert from "node:assert/strict";
import {
  FALLBACK_FILMS,
  PLACEHOLDER_PARAGRAPHS,
  fallbackFilmFor,
  pickActiveShot,
  resolveFilmSrc,
} from "./fallback.ts";
import type { ShotResponse } from "../lib/api.ts";

const shot = (id: string, start: number, end: number): ShotResponse => ({
  shot_id: id,
  status: "ready",
  duration_s: 4,
  clip_url: null,
  source_span: { word_range: [start, end] },
});

test("fallbackFilmFor always returns a bundled film path", () => {
  for (const id of ["", "a", "demo-book", "00000000-1111-2222"]) {
    assert.ok(FALLBACK_FILMS.includes(fallbackFilmFor(id)));
  }
});

test("fallbackFilmFor is deterministic for the same id", () => {
  assert.equal(fallbackFilmFor("the-midnight-library"), fallbackFilmFor("the-midnight-library"));
});

test("fallbackFilmFor maps by char-sum hash (golden)", () => {
  // "a" = 97 → 97 % 4 = 1 ; "ab" = 195 → 195 % 4 = 3
  assert.equal(fallbackFilmFor("a"), FALLBACK_FILMS[1]);
  assert.equal(fallbackFilmFor("ab"), FALLBACK_FILMS[3]);
  // empty id → index 0
  assert.equal(fallbackFilmFor(""), FALLBACK_FILMS[0]);
});

test("PLACEHOLDER_PARAGRAPHS gives readable fallback prose", () => {
  assert.ok(PLACEHOLDER_PARAGRAPHS.length >= 3);
  assert.ok(PLACEHOLDER_PARAGRAPHS.every((p) => p.length > 20));
});

test("pickActiveShot returns the greatest shot whose span starts at/before the focus word", () => {
  const shots = [shot("s1", 0, 50), shot("s2", 50, 120), shot("s3", 120, 200)];
  assert.equal(pickActiveShot(shots, 0)?.shot_id, "s1");
  assert.equal(pickActiveShot(shots, 70)?.shot_id, "s2");
  assert.equal(pickActiveShot(shots, 500)?.shot_id, "s3"); // past the end → last shot
});

test("pickActiveShot returns null for empty shots or focus before the first shot", () => {
  assert.equal(pickActiveShot([], 10), null);
  assert.equal(pickActiveShot([shot("s1", 30, 60)], 10), null); // focus before first span
});

test("resolveFilmSrc paints the bundled film when not live", () => {
  const r = resolveFilmSrc({ live: false, activeClip: undefined, fallbackFilm: "/f.mp4", hasShownFrame: false });
  assert.equal(r.src, "/f.mp4");
  assert.equal(r.generating, false);
});

test("resolveFilmSrc paints the live clip when one is ready", () => {
  const r = resolveFilmSrc({ live: true, activeClip: "/clip.mp4", fallbackFilm: "/f.mp4", hasShownFrame: true });
  assert.equal(r.src, "/clip.mp4");
  assert.equal(r.generating, false);
});

test("resolveFilmSrc bootstraps with the bundled film (never black) before the first live clip", () => {
  const r = resolveFilmSrc({ live: true, activeClip: undefined, fallbackFilm: "/f.mp4", hasShownFrame: false });
  assert.equal(r.src, "/f.mp4"); // never an empty/black first frame
  assert.equal(r.generating, true); // still tell the reader we're generating ahead
});

test("resolveFilmSrc holds the last frame (no swap) once something has played and the next clip lags", () => {
  const r = resolveFilmSrc({ live: true, activeClip: undefined, fallbackFilm: "/f.mp4", hasShownFrame: true });
  assert.equal(r.src, ""); // empty src = CrossfadeFilm keeps the current frame on screen
  assert.equal(r.generating, true);
});
