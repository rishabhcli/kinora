// The no-live-video path. With KINORA_LIVE_VIDEO OFF (the default) the scheduler
// never promotes live renders, so the film is a bundled Ken-Burns mp4 + canned
// prose — and it must look and scrub beautifully. Pure + unit-tested.
import type { ShotResponse } from "../lib/api";

/** Bundled real Wan films shipped in /public/generated — the fallback for
 *  mock-catalogue books with no backend. */
export const FALLBACK_FILMS: readonly string[] = [
  "/generated/film-01.mp4",
  "/generated/film-02.mp4",
  "/generated/film-03.mp4",
  "/generated/film-04.mp4",
];

/** Canned prose shown when there is no backend text — three short paragraphs so
 *  the reading column is never empty. */
export const PLACEHOLDER_PARAGRAPHS: readonly string[] = [
  "The first page felt heavy in her hands, as if the weight of every possible life pressed against her fingertips.",
  "Each book was a door, and each door led to a different version of the story — paths not taken, words not yet spoken.",
  "As the pages turned, the world rearranged itself a few seconds ahead, the way a film assembles just before you arrive.",
];

/** Deterministically pick a bundled film for a book id (stable per book). */
export function fallbackFilmFor(bookId: string): string {
  const h = [...bookId].reduce((a, c) => a + c.charCodeAt(0), 0);
  return FALLBACK_FILMS[h % FALLBACK_FILMS.length] as string;
}

/** Active shot = the greatest shot whose span starts at/before the focus word.
 *  Assumes `shots` are sorted ascending by `source_span.word_range[0]`. */
export function pickActiveShot(shots: readonly ShotResponse[], focusWord: number): ShotResponse | null {
  let active: ShotResponse | null = null;
  for (const s of shots) {
    if (s.source_span && s.source_span.word_range[0] <= focusWord) active = s;
    else break;
  }
  return active;
}

export interface FilmSrcInput {
  live: boolean;
  activeClip: string | undefined;
  fallbackFilm: string;
  /** Has a real frame already painted? (so we can hold it instead of going black) */
  hasShownFrame: boolean;
}

export interface FilmSrc {
  /** "" means: keep the current frame on screen (CrossfadeFilm holds the last layer). */
  src: string;
  /** Show the "generating ahead" affordance (the live clip isn't ready yet). */
  generating: boolean;
}

/** Choose what the film surface paints — never an empty/black first frame. */
export function resolveFilmSrc({ live, activeClip, fallbackFilm, hasShownFrame }: FilmSrcInput): FilmSrc {
  if (!live) return { src: fallbackFilm, generating: false };
  if (activeClip) return { src: activeClip, generating: false };
  // Live, but the active shot's clip isn't ready yet.
  return hasShownFrame
    ? { src: "", generating: true } // something already played — hold that frame
    : { src: fallbackFilm, generating: true }; // bootstrap with the bundled film so the first frame is never black
}
