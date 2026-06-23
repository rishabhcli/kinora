/**
 * @kinora/core — shared logic for the Kinora desktop (Electron) and mobile (Expo) apps.
 *
 * Framework-agnostic TypeScript, consumed directly as source by both apps
 * (Vite on desktop, Metro on mobile). Holds the typed API client (generated
 * from the backend OpenAPI schema), and — landing next — the SyncEngine,
 * Zustand stores, Zod event schemas, and the WebSocket/SSE clients.
 */

export const CORE_VERSION = "0.1.0";

/** The two shells that consume this core. */
export type Platform = "desktop" | "mobile";

export * from "./api/client";
export type * from "./api/types";
export type { paths, components, operations } from "./api/schema";
