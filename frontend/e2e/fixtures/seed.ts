// Deterministic constants for the seeded e2e book.
//
// These MUST stay in lock-step with `backend/scripts/seed_e2e.py` (the seeder
// that writes the book the specs read). They are the contract between the
// Python seeder and the TypeScript specs.

export const SEED = {
  /** The seeded user the shelf/workspace specs log in as (owns the book). */
  email: "e2e@kinora.test",
  password: "e2e-password-123",
  /** The seeded book's title — the shelf spec finds the card by this. */
  bookTitle: "The Frog-King (e2e seed)",
  /** Pages rasterised from the demo PDF (book pages 1..N). */
  numPages: 3,
  /** The §4.2 source-span grid step: shot i starts at word i*wordStep. */
  wordStep: 10,
  /** The one accepted shot with a real, playable Ken-Burns clip. */
  acceptedShotId: "shot_0000",
  /** A planned (clip-less) shot used to assert the clip_ready source-swap. */
  plannedShotId: "shot_0001",
  /** Another planned shot used to assert the keyframe_ready bridge. */
  keyframeShotId: "shot_0002",
  /** Style canon node the director-mode canon-edit targets (no model spend). */
  styleEntityKey: "style_storybook",
} as const;

/** The displayed timeline label for a shot id (ShotTimeline strips "shot_"). */
export function shotLabel(shotId: string): string {
  return shotId.replace(/^shot_?/, "#");
}
