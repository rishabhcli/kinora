/**
 * @kinora/core — shared logic for the Kinora desktop (Electron) and mobile (Expo) apps.
 *
 * Framework-agnostic TypeScript, consumed directly as source by both apps
 * (Vite on desktop, Metro on mobile): the typed API client (generated from the
 * backend OpenAPI schema), the §5.6 event schemas, and the sync primitives that
 * back the playhead. The stateful SyncEngine, realtime socket, and stores land
 * on top of these.
 */

export const CORE_VERSION = "0.1.0";

/** The two shells that consume this core. */
export type Platform = "desktop" | "mobile";

export * from "./api/client";
export type * from "./api/types";
export type { paths, components, operations } from "./api/schema";

export * from "./events";
export * from "./sync/velocity";
export * from "./sync/timeline";
export * from "./sync/SyncEngine";
export * from "./realtime/socket";
