// A thin Picture-in-Picture controller for the film pane. PiP lets a reader pop
// the AI film into a floating always-on-top window and keep reading the text full
// width — a genuinely nice fit for a generation-on-scroll reader. The browser API
// (HTMLVideoElement.requestPictureInPicture / document.exitPictureInPicture) is
// well-supported in Chromium/Electron but absent under reduced environments, so we
// detect and degrade.
//
// The DOM touch is unavoidable; we keep it structural (a `PipVideo` slice) so a
// test drives it with a stub and asserts the state machine + capability gate.
// Pure scrub/quality logic stays elsewhere.

export interface PipVideo {
  requestPictureInPicture?: () => Promise<unknown>;
  disablePictureInPicture?: boolean;
  readyState?: number;
}

export interface PipDocument {
  pictureInPictureEnabled?: boolean;
  pictureInPictureElement?: unknown;
  exitPictureInPicture?: () => Promise<void>;
}

export type PipState = "unavailable" | "inactive" | "active";

/** Is PiP usable for this video + document right now? */
export function canUsePip(video: PipVideo | null, doc: PipDocument | null): boolean {
  if (!video || !doc) return false;
  if (doc.pictureInPictureEnabled === false) return false;
  if (video.disablePictureInPicture === true) return false;
  return typeof video.requestPictureInPicture === "function";
}

/** Derive the current PiP state. */
export function pipState(video: PipVideo | null, doc: PipDocument | null): PipState {
  if (!canUsePip(video, doc)) return "unavailable";
  return doc?.pictureInPictureElement ? "active" : "inactive";
}

/** Enter PiP for `video`. Resolves false (no throw) when unavailable or rejected
 *  — the caller keeps the inline film. */
export async function enterPip(video: PipVideo | null, doc: PipDocument | null): Promise<boolean> {
  if (!canUsePip(video, doc) || !video?.requestPictureInPicture) return false;
  try {
    await video.requestPictureInPicture();
    return true;
  } catch {
    return false;
  }
}

/** Exit PiP if active. Resolves false when there was nothing to exit. */
export async function exitPip(doc: PipDocument | null): Promise<boolean> {
  if (!doc?.pictureInPictureElement || typeof doc.exitPictureInPicture !== "function") return false;
  try {
    await doc.exitPictureInPicture();
    return true;
  } catch {
    return false;
  }
}

/** Toggle: enter if inactive, exit if active. Returns the resulting state. */
export async function togglePip(video: PipVideo | null, doc: PipDocument | null): Promise<PipState> {
  const state = pipState(video, doc);
  if (state === "unavailable") return "unavailable";
  if (state === "active") {
    await exitPip(doc);
    return "inactive";
  }
  const ok = await enterPip(video, doc);
  return ok ? "active" : "inactive";
}
