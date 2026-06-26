// Per-launch backdrop variation (stretch): the screening room is lit a little
// differently each visit — a different projector angle + warmth — so the login
// feels alive without ever being random/janky. Deterministic from a seed so a
// given launch is stable across re-renders. Pure; tested under node --test.

export interface BackdropVariant {
  name: string;
  /** projector beam angle, degrees from vertical */
  beamAngle: number;
  /** horizontal origin of the beam, 0..100 (% of viewport width) */
  beamX: number;
  /** warmth multiplier for the key light, ~0.85..1.2 */
  warmth: number;
  /** parallax intensity multiplier for the shelves, ~0.8..1.25 */
  parallax: number;
}

// A small, hand-tuned set — every one reads as the same room, just a different
// hour of the evening. Order matters: consecutive seeds cycle through them.
export const BACKDROP_VARIANTS: readonly BackdropVariant[] = [
  { name: "matinee", beamAngle: -10, beamX: 38, warmth: 1.0, parallax: 1.0 },
  { name: "dusk", beamAngle: -16, beamX: 50, warmth: 1.12, parallax: 1.15 },
  { name: "lamplight", beamAngle: -6, beamX: 62, warmth: 0.92, parallax: 0.9 },
  { name: "premiere", beamAngle: -13, beamX: 44, warmth: 1.06, parallax: 1.25 },
];

// Stable string hash (FNV-1a, 32-bit) so book-id / date seeds map consistently.
function hashSeed(seed: number | string): number {
  if (typeof seed === "number") return Math.abs(Math.trunc(seed));
  let h = 0x811c9dc5;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return Math.abs(h);
}

/** Deterministically pick a variant for a given seed (number or string). */
export function pickBackdropVariant(seed: number | string): BackdropVariant {
  const i = hashSeed(seed) % BACKDROP_VARIANTS.length;
  return BACKDROP_VARIANTS[i];
}
