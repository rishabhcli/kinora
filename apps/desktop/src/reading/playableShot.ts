import type { ShotResponse } from "../lib/api";

const SYNTHETIC_RENDER_MODES = new Set([
  "degraded",
  "image",
  "ken_burns",
  "ken_burns_keyframe",
  "poster",
  "still",
]);

/** Only critic-accepted hosted video is allowed onto the reader's film surface. */
export function isPlayableShot(shot: ShotResponse): boolean {
  const mode = shot.render_mode?.trim().toLowerCase();
  return shot.status === "accepted" && Boolean(shot.clip_url) && (!mode || !SYNTHETIC_RENDER_MODES.has(mode));
}
