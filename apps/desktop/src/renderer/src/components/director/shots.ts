/**
 * The Director timeline's view-model lives in `@kinora/core` (`director.ts`) so
 * both shells share one implementation; this is a stable local import path for
 * the desktop renderer. See kinora.md §5.4.
 */
export {
  sceneWindow,
  toDirectorShots,
  type DirectorShot,
  type ShotTileStatus,
  type ShotUpdate,
  type ShotUpdateMap,
} from "@kinora/core";
