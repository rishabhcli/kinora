/**
 * Pure model for the Canon editor (§5.4 / §8), shared by both shells: how a canon
 * entity projects into an editable draft, how a draft diffs back into a
 * `canon_edit` `changes` payload, and how a shot's reference set decides whether
 * an entity edit re-renders it (§8.7 — the surgical-regen blast radius, mirrored
 * from the backend's `_references_entity`). No UI here, so the diff logic stays
 * unit-checkable and reusable across desktop + mobile.
 */
import type { CanonEntityResponse, ShotResponse } from "./api/types";
import { summarizeQa } from "./feed";

/** The canon entity kinds, in the order the editor lists them. */
export const ENTITY_GROUPS: { type: string; label: string; blurb: string }[] = [
  { type: "character", label: "Characters", blurb: "Appearance + locked reference set" },
  { type: "location", label: "Locations", blurb: "Place look + references" },
  { type: "prop", label: "Props", blurb: "Objects the camera must keep consistent" },
  { type: "style", label: "Style", blurb: "Palette · lens · art direction" },
];

export interface RefDraft {
  /** Durable object-store key — echoed back on save so the swap round-trips. */
  ossKey: string | null;
  /** Presigned URL for display only (cannot be re-stored). */
  ossUrl: string;
  pose: string | null;
  locked: boolean;
}

export interface EntityDraft {
  name: string;
  aliasesText: string;
  description: string;
  appearanceDescription: string;
  references: RefDraft[];
  palette: string[];
  lens: string;
  artDirection: string;
}

/** The outcome of one applied canon edit — drives the dependent-shots strip. */
export interface EditResult {
  entityKey: string;
  entityName: string;
  entityType: string;
  version: number;
  affectedShotIds: string[];
  skipped: number;
  at: number;
}

/** Strip the `@vN` version suffix from a shot's reference id → the entity key. */
export function baseEntityKey(refId: string): string {
  return refId.split("@", 1)[0] ?? refId;
}

/**
 * The shots whose reference set cites `entityKey` — exactly the set the backend
 * re-renders on a canon edit (§8.7). Everything else stays a cache hit.
 */
export function dependentShotIds(
  shots: ShotResponse[] | undefined,
  entityKey: string,
): string[] {
  if (!shots) return [];
  return shots
    .filter((s) => (s.reference_image_ids ?? []).some((r) => baseEntityKey(r) === entityKey))
    .map((s) => s.shot_id);
}

export function parseAliases(text: string): string[] {
  return text
    .split(",")
    .map((a) => a.trim())
    .filter(Boolean);
}

/** Normalize a style `palette` token (array, comma string, or absent) to colors. */
export function parsePaletteValue(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean);
  if (typeof value === "string") {
    return value
      .split(/[\s,]+/)
      .map((v) => v.trim())
      .filter(Boolean);
  }
  return [];
}

function styleString(tokens: Record<string, unknown> | null | undefined, key: string): string {
  const value = tokens?.[key];
  return typeof value === "string" ? value : "";
}

function arraysEqual(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** Seed an editable draft from the entity's current (latest) version. */
export function draftFromEntity(entity: CanonEntityResponse): EntityDraft {
  const tokens = entity.style_tokens ?? null;
  return {
    name: entity.name,
    aliasesText: (entity.aliases ?? []).join(", "),
    description: entity.description ?? "",
    appearanceDescription: entity.appearance?.description ?? "",
    references: (entity.appearance?.reference_images ?? []).map((r) => ({
      ossKey: r.oss_key ?? null,
      ossUrl: r.oss_url,
      pose: r.pose ?? null,
      locked: Boolean(r.locked),
    })),
    palette: parsePaletteValue(tokens?.palette),
    lens: styleString(tokens, "lens"),
    artDirection: styleString(tokens, "art_direction"),
  };
}

/**
 * Diff a draft against its entity into a minimal `canon_edit` `changes` map.
 * Only changed fields appear; the backend falls every omitted field back to the
 * current value. An empty result means "nothing to save".
 *
 * `changes.appearance` *replaces* the whole appearance block, so it's rebuilt in
 * full from the draft (description + the locked reference set, keyed by the
 * durable `oss_key`) whenever the appearance is touched.
 */
export function buildChanges(
  entity: CanonEntityResponse,
  draft: EntityDraft,
): Record<string, unknown> {
  const changes: Record<string, unknown> = {};

  const name = draft.name.trim();
  if (name && name !== entity.name) changes.name = name;

  const aliases = parseAliases(draft.aliasesText);
  if (!arraysEqual(aliases, entity.aliases ?? [])) changes.aliases = aliases;

  const description = draft.description.trim();
  if (description !== (entity.description ?? "").trim()) {
    changes.description = description || null;
  }

  const appDescription = draft.appearanceDescription.trim();
  const currentAppDescription = (entity.appearance?.description ?? "").trim();
  const originalRefs = entity.appearance?.reference_images ?? [];
  const locksChanged = draft.references.some(
    (r, i) => r.locked !== Boolean(originalRefs[i]?.locked),
  );
  const hasAppearance =
    entity.appearance != null || appDescription.length > 0 || draft.references.length > 0;
  if (hasAppearance && (appDescription !== currentAppDescription || locksChanged)) {
    changes.appearance = {
      description: appDescription || null,
      reference_images: draft.references
        .filter((r) => r.ossKey)
        .map((r) => ({
          key: r.ossKey,
          ...(r.pose ? { pose: r.pose } : {}),
          locked: r.locked,
        })),
    };
  }

  if (entity.type === "style") {
    const tokens = entity.style_tokens ?? {};
    const palette = draft.palette;
    const lens = draft.lens.trim();
    const artDirection = draft.artDirection.trim();
    const paletteChanged = !arraysEqual(palette, parsePaletteValue(tokens.palette));
    const lensChanged = lens !== styleString(tokens, "lens");
    const artChanged = artDirection !== styleString(tokens, "art_direction");
    if (paletteChanged || lensChanged || artChanged) {
      changes.style_tokens = {
        ...tokens,
        palette,
        ...(lens ? { lens } : {}),
        ...(artDirection ? { art_direction: artDirection } : {}),
      };
    }
  }

  return changes;
}

export interface CcsDelta {
  before: number | null;
  after: number | null;
  /** Whether the score held (after ≥ before); null when either side is unknown. */
  held: boolean | null;
}

/**
 * Pair a shot's prior QA with its post-regen QA so the editor can show the
 * character-consistency score before → after an edit — the proof that a surgical
 * re-render kept the look consistent (§9.5). Tolerant of the §8.2 qa shape.
 */
export function ccsDelta(
  beforeQa: Record<string, unknown> | null | undefined,
  afterQa: Record<string, unknown> | null | undefined,
): CcsDelta {
  const before = summarizeQa(beforeQa ?? null)?.ccs ?? null;
  const after = summarizeQa(afterQa ?? null)?.ccs ?? null;
  const held = before != null && after != null ? after + 1e-9 >= before : null;
  return { before, after, held };
}

/**
 * Build a `canon_edit` `changes` payload that restores an entity to a prior
 * snapshot — the Undo of a surgical edit (re-bumps the version, re-renders the
 * same dependent shots back to how they were). The appearance is reconstructed
 * in full by durable `oss_key` so locked-reference swaps undo losslessly.
 */
export function changesToRestore(prior: CanonEntityResponse): Record<string, unknown> {
  const changes: Record<string, unknown> = {
    name: prior.name,
    aliases: prior.aliases ?? [],
    description: prior.description ?? null,
  };
  if (prior.appearance) {
    changes.appearance = {
      description: prior.appearance.description ?? null,
      reference_images: (prior.appearance.reference_images ?? [])
        .filter((r) => r.oss_key)
        .map((r) => ({
          key: r.oss_key,
          ...(r.pose ? { pose: r.pose } : {}),
          locked: Boolean(r.locked),
        })),
    };
  }
  if (prior.type === "style" && prior.style_tokens) {
    changes.style_tokens = prior.style_tokens;
  }
  return changes;
}

/**
 * Apply an edit to an entity locally for an instant, flicker-free update (§5.4),
 * reconciled by the canon refetch. Bumps the version + the changed scalar fields,
 * and rebuilds the appearance from the original refs (which carry the display
 * URL) so a locked-reference swap shows immediately. Shared by both shells.
 */
export function applyOptimisticEdit(
  entity: CanonEntityResponse,
  changes: Record<string, unknown>,
): CanonEntityResponse {
  const next: CanonEntityResponse = { ...entity, version: entity.version + 1 };
  if (typeof changes.name === "string") next.name = changes.name;
  if (Array.isArray(changes.aliases)) next.aliases = changes.aliases as string[];
  if ("description" in changes) next.description = (changes.description as string | null) ?? null;
  if (changes.style_tokens && typeof changes.style_tokens === "object") {
    next.style_tokens = changes.style_tokens as Record<string, unknown>;
  }
  const app = changes.appearance as
    | { description?: string | null; reference_images?: { key?: string; pose?: string; locked?: boolean }[] }
    | undefined;
  if (app) {
    const origByKey = new Map((entity.appearance?.reference_images ?? []).map((r) => [r.oss_key, r]));
    next.appearance = {
      description: app.description ?? null,
      reference_images: (app.reference_images ?? []).map((r) => {
        const orig = r.key ? origByKey.get(r.key) : undefined;
        return {
          oss_url: orig?.oss_url ?? "",
          oss_key: r.key ?? null,
          pose: r.pose ?? null,
          locked: Boolean(r.locked),
        };
      }),
    };
  }
  return next;
}

/**
 * Guardrails on a canon edit (§8.1): never save an empty name, and never unlock
 * an entity's *entire* locked reference set — that erases the visual identity
 * every dependent shot is conditioned on. Returns human-readable problems; an
 * empty array means the draft is safe to save.
 */
export function validateCanonDraft(entity: CanonEntityResponse, draft: EntityDraft): string[] {
  const errors: string[] = [];
  if (!draft.name.trim()) errors.push("Name can't be empty.");
  const hadLocked = (entity.appearance?.reference_images ?? []).some((r) => r.locked);
  const hasLocked = draft.references.some((r) => r.locked);
  if (hadLocked && draft.references.length > 0 && !hasLocked) {
    errors.push("Keep at least one reference locked — it pins this entity's identity.");
  }
  return errors;
}
