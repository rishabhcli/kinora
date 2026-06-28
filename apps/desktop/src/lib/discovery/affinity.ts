// Taste-profile builder — turns the interaction history into per-genre/era/author
// affinity weights with recency decay. Pure: same history → same profile.
import type { Interaction, InteractionKind, TasteProfile } from "./types";

/** How strong each interaction kind is as a taste signal. Dismiss is negative. */
export const KIND_WEIGHTS: Record<InteractionKind, number> = {
  view: 0.15,
  hover: 0.25,
  preview: 1,
  open: 3,
  finish: 5,
  favorite: 4,
  dismiss: -6,
};

/** Half-life of a signal, in days: a 14-day-old open counts ~half a fresh one. */
const HALF_LIFE_DAYS = 14;
const MS_PER_DAY = 86_400_000;

/** Exponential recency decay in (0,1]. `now`/`at` are epoch-ms. */
export function recencyDecay(at: number, now: number, halfLifeDays = HALF_LIFE_DAYS): number {
  const ageDays = Math.max(0, (now - at) / MS_PER_DAY);
  return Math.pow(0.5, ageDays / halfLifeDays);
}

function bump(map: Record<string, number>, key: string | undefined, delta: number): void {
  if (!key) return;
  map[key] = (map[key] ?? 0) + delta;
}

/**
 * Build the reader's taste profile from history. Each interaction contributes
 * `kindWeight * recencyDecay` to the genre/era/author it carries. A `dismiss`
 * also adds the book id to `dismissed` so it's excluded from recommendations.
 */
export function buildProfile(
  history: Interaction[],
  opts: { now?: number; halfLifeDays?: number } = {},
): TasteProfile {
  const now = opts.now ?? Date.now();
  const halfLife = opts.halfLifeDays ?? HALF_LIFE_DAYS;

  const profile: TasteProfile = {
    genres: {},
    eras: {},
    authors: {},
    dismissed: new Set<string>(),
    totalSignal: 0,
  };

  for (const ev of history) {
    const w = KIND_WEIGHTS[ev.kind] ?? 0;
    const decayed = w * recencyDecay(ev.at, now, halfLife);
    bump(profile.genres, ev.genre, decayed);
    bump(profile.eras, ev.era, decayed);
    bump(profile.authors, ev.author, decayed);
    if (ev.kind === "dismiss") profile.dismissed.add(ev.bookId);
    if (decayed > 0) profile.totalSignal += decayed;
  }

  return profile;
}

/** A reader with essentially no positive signal yet — show editorial/popular
 *  rows instead of personalized ones. */
export function isColdStart(profile: TasteProfile, threshold = 1): boolean {
  return profile.totalSignal < threshold;
}

/** Top-N keys of an affinity map, descending, dropping non-positive weights. */
export function topAffinities(map: Record<string, number>, n = 3): string[] {
  return Object.entries(map)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, n)
    .map(([k]) => k);
}

/** The reader's single strongest genre (for a "More <genre>" row), or null. */
export function favoriteGenre(profile: TasteProfile): string | null {
  return topAffinities(profile.genres, 1)[0] ?? null;
}
