// Data + pure helpers for the on-device "two-pane reading workspace" preview.
// Everything here is deterministic and framework-free so it can be unit-tested
// without rendering. The excerpt is public-domain (Alice's Adventures in
// Wonderland, Lewis Carroll, 1865), matching the roadmap's "short, public-domain
// illustrated story" target.

export const EXCERPT_TITLE = "Alice's Adventures in Wonderland";
export const EXCERPT_BYLINE = "Lewis Carroll · public domain";

export interface SceneArt {
  /** Colour of the soft "sun/orb" glow in the faux shot. */
  orb: string;
  /** Top colour of the sky gradient. */
  skyTop: string;
  /** Bottom colour of the sky gradient. */
  skyBottom: string;
  /** Ground / foreground colour. */
  ground: string;
  /** Whether to draw a motion streak (used for the Rabbit's dash). */
  motion?: boolean;
}

export interface Beat {
  id: number;
  /** The exact book text this shot covers. */
  text: string;
  /** A short, on-brand camera/scene direction shown on the film pane. */
  shot: string;
  scene: SceneArt;
}

export interface Token {
  word: string;
  beat: number;
}

export const beats: Beat[] = [
  {
    id: 0,
    text: "Alice was beginning to get very tired of sitting by her sister on the bank,",
    shot: "Wide — a sunlit riverbank, late afternoon",
    scene: { orb: "#ffd58a", skyTop: "#7c6a3f", skyBottom: "#1c2a1f", ground: "#2f6b4f" },
  },
  {
    id: 1,
    text: "and of having nothing to do: once or twice she had peeped into the book her sister was reading,",
    shot: "Insert — an open book, pages turning",
    scene: { orb: "#c9b48a", skyTop: "#3a3350", skyBottom: "#15131f", ground: "#4a4068" },
  },
  {
    id: 2,
    text: "but it had no pictures or conversations in it, and what is the use of a book, thought Alice, without pictures or conversations?",
    shot: "Close on Alice — bored, considering",
    scene: { orb: "#9fb6ff", skyTop: "#2a3358", skyBottom: "#101324", ground: "#27407a" },
  },
  {
    id: 3,
    text: "So she was considering in her own mind, when suddenly a White Rabbit with pink eyes ran close by her.",
    shot: "Fast pan — the White Rabbit darts past",
    scene: { orb: "#eafff0", skyTop: "#3f6f55", skyBottom: "#10231a", ground: "#2c8a5d", motion: true },
  },
];

/** Flatten beats into a whitespace-split token stream, tagging each word with
 *  its beat index. */
export function tokenize(input: Beat[] = beats): Token[] {
  const tokens: Token[] = [];
  for (const beat of input) {
    for (const word of beat.text.split(/\s+/).filter(Boolean)) {
      tokens.push({ word, beat: beat.id });
    }
  }
  return tokens;
}

export type Zone = "played" | "playing" | "committed" | "speculative" | "cold";

/** Classify a shot by its distance ahead of the current playhead. This is the
 *  watermark-buffer idea in miniature: the playhead's shot is committed, the
 *  next is committed-ahead, then speculative (keyframe only), then cold. */
export function zoneForOffset(offset: number): Zone {
  if (offset < 0) return "played";
  if (offset === 0) return "playing";
  if (offset === 1) return "committed";
  if (offset === 2) return "speculative";
  return "cold";
}

export function clampIndex(index: number, length: number): number {
  if (length <= 0) return 0;
  return Math.min(Math.max(index, 0), length - 1);
}
