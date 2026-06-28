// Described-video model, pure half. For blind / low-vision readers the film needs
// an audio-description track: short spoken descriptions of what the generated shot
// shows, anchored to the timeline so they fire as the reader reaches each shot.
// Kinora already has the material — the Cinematographer's shot brief / scene
// summary (kinora.md §9.4) — so a description cue is just that text anchored to a
// shot's word range. This module resolves which cue is active for a focus word,
// prevents re-announcing the same cue, and (given a cue's spoken duration vs. the
// reading pace) flags when a description would overrun the next one so the engine
// can prioritise / truncate. It feeds the existing a11y/announce + a11y/tts
// machinery; it owns no DOM and no speech synthesis.

export interface DescriptionCue {
  /** stable id (usually the shot_id) */
  id: string;
  /** the spoken description text */
  text: string;
  /** global word index where this cue becomes active (inclusive) */
  wordStart: number;
  /** global word index where it stops being active (exclusive) */
  wordEnd: number;
  /** optional priority: higher wins when two cues overlap (default 0) */
  priority?: number;
}

/** A description track = cues sorted by wordStart, made non-overlapping like the
 *  timeline (a cue runs until the next cue's start). */
export interface DescriptionTrack {
  cues: DescriptionCue[];
}

/** Build a track from raw cues: sort, drop empties, make contiguous. Mirrors
 *  buildTimeline's contiguity so a focus word always resolves to exactly one cue. */
export function buildTrack(cues: DescriptionCue[]): DescriptionTrack {
  const cleaned = cues.filter((c) => c.text.trim().length > 0).sort((a, b) => a.wordStart - b.wordStart);
  for (let i = 0; i < cleaned.length - 1; i++) {
    // Each cue runs up to the next one's start — contiguous like the timeline, so
    // every focus word in range resolves to exactly one cue (no gaps, no overlaps).
    cleaned[i] = { ...cleaned[i], wordEnd: cleaned[i + 1].wordStart };
  }
  return { cues: cleaned };
}

/** The cue active for a focus word: the greatest cue whose wordStart ≤ focus
 *  (matching resolvePlayhead's rule). Null for an empty track / before the first. */
export function activeCue(track: DescriptionTrack, focusWord: number): DescriptionCue | null {
  const { cues } = track;
  let found: DescriptionCue | null = null;
  for (const c of cues) {
    if (c.wordStart <= focusWord) found = c;
    else break;
  }
  return found;
}

/** Estimate how long a description takes to speak, in seconds, at `wordsPerMinute`
 *  (typical TTS narration ≈ 170 wpm). Used to decide whether a cue fits before the
 *  reader reaches the next shot. */
export function spokenDurationS(text: string, wordsPerMinute = 170): number {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  if (words === 0 || wordsPerMinute <= 0) return 0;
  return (words / wordsPerMinute) * 60;
}

export interface AnnouncerState {
  /** the id of the cue we most recently announced (so we don't repeat it) */
  lastAnnouncedId: string | null;
}

export interface AnnounceDecision {
  /** the cue to speak now, or null (nothing new to announce) */
  cue: DescriptionCue | null;
  /** next announcer state to carry forward */
  next: AnnouncerState;
}

/** Decide whether to announce a new description for the current focus word. Only
 *  announces when the active cue CHANGED (so a reader dwelling in a shot isn't
 *  re-told what it shows). Pure: feed it the prior state, get the cue + next state.
 *  The DOM adapter pipes `cue.text` into a11y/announce or a11y/tts. */
export function decideAnnounce(
  track: DescriptionTrack,
  focusWord: number,
  state: AnnouncerState,
): AnnounceDecision {
  const cue = activeCue(track, focusWord);
  if (!cue) return { cue: null, next: state };
  if (cue.id === state.lastAnnouncedId) return { cue: null, next: state };
  return { cue, next: { lastAnnouncedId: cue.id } };
}

export const initialAnnouncerState: AnnouncerState = { lastAnnouncedId: null };
