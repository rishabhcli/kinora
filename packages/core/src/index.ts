/**
 * @kinora/core — shared logic for the Kinora desktop (Electron) and mobile (Expo) apps.
 *
 * Framework-agnostic TypeScript, consumed directly as source by both apps
 * (Vite on desktop, Metro on mobile). This package will hold the SyncEngine,
 * the typed API client (generated from the backend OpenAPI schema), Zustand
 * stores, Zod schemas, and the WebSocket/SSE clients.
 */

export const CORE_VERSION = "0.1.0";

/** The two shells that consume this core. */
export type Platform = "desktop" | "mobile";
