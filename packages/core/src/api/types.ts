/**
 * Convenience aliases for the backend's request/response models, sourced from
 * the OpenAPI-generated {@link components} (see ./schema.ts — regenerate with
 * `pnpm --filter @kinora/core gen:api`). Import these instead of reaching into
 * `components["schemas"][...]` everywhere.
 */
import type { components } from "./schema";

type Schemas = components["schemas"];

// --- Auth ---
export type RegisterRequest = Schemas["RegisterRequest"];
export type LoginRequest = Schemas["LoginRequest"];
export type TokenResponse = Schemas["TokenResponse"];
export type UserResponse = Schemas["UserResponse"];

// --- Library ---
export type BookResponse = Schemas["BookResponse"];
export type PageResponse = Schemas["PageResponse"];
export type ShotResponse = Schemas["ShotResponse"];

// --- Canon ---
export type CanonResponse = Schemas["CanonResponse"];
export type CanonEntityResponse = Schemas["CanonEntityResponse"];
export type CanonAppearance = Schemas["CanonAppearance"];
export type CanonReferenceImage = Schemas["CanonReferenceImage"];
export type CanonEditRequest = Schemas["CanonEditRequest"];
export type CanonEditResponse = Schemas["CanonEditResponse"];

// --- Sessions / playback ---
export type CreateSessionRequest = Schemas["CreateSessionRequest"];
export type SessionResponse = Schemas["SessionResponse"];
export type IntentRequest = Schemas["IntentRequest"];
export type IntentResponse = Schemas["IntentResponse"];
export type SeekRequest = Schemas["SeekRequest"];
export type SeekResponse = Schemas["SeekResponse"];

// --- Director ---
export type CommentRequest = Schemas["CommentRequest"];
export type CommentResponse = Schemas["CommentResponse"];
export type ConflictChoiceRequest = Schemas["ConflictChoiceRequest"];
export type ConflictChoiceResponse = Schemas["ConflictChoiceResponse"];

// --- Eval ---
export type BufferTracePoint = Schemas["BufferTracePoint"];
