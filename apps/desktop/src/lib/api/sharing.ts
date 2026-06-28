// Sharing + export (Director domain) — produce shareable references to a book /
// scene / shot and portable export bundles for the Director's work (canon,
// annotations, collections). No backend share endpoint exists yet, so links are
// deep-link descriptors the app can resolve internally (and a future backend can
// mint signed URLs from); exports are self-describing JSON. All pure + testable.
import type { AnnotationExport } from "./annotations";
import type { SmartCollection } from "./collections";
import type { CanonGraph } from "./director";

// ---- Deep links ----------------------------------------------------------- //

/** A resolvable in-app target. The app router turns this into a route; a backend
 *  could turn it into a signed public URL. Encoded as a compact query string. */
export interface ShareTarget {
  kind: "book" | "scene" | "shot";
  book_id: string;
  scene_id?: string;
  shot_id?: string;
  /** Optional film-timeline second to deep-link a moment. */
  t?: number;
}

const SCHEME = "kinora";

/** Encode a target into a `kinora://` deep link (stable, URL-safe). */
export function encodeShareLink(target: ShareTarget): string {
  const params = new URLSearchParams();
  params.set("book", target.book_id);
  if (target.scene_id) params.set("scene", target.scene_id);
  if (target.shot_id) params.set("shot", target.shot_id);
  if (typeof target.t === "number" && Number.isFinite(target.t)) params.set("t", String(target.t));
  return `${SCHEME}://${target.kind}?${params.toString()}`;
}

/** Decode a `kinora://` deep link back into a target, or null if malformed. */
export function decodeShareLink(link: string): ShareTarget | null {
  if (!link.startsWith(`${SCHEME}://`)) return null;
  const rest = link.slice(`${SCHEME}://`.length);
  const qIdx = rest.indexOf("?");
  if (qIdx < 0) return null;
  const kind = rest.slice(0, qIdx);
  if (kind !== "book" && kind !== "scene" && kind !== "shot") return null;
  const params = new URLSearchParams(rest.slice(qIdx + 1));
  const book_id = params.get("book");
  if (!book_id) return null;
  const tRaw = params.get("t");
  const t = tRaw !== null && Number.isFinite(Number(tRaw)) ? Number(tRaw) : undefined;
  const target: ShareTarget = { kind, book_id };
  const scene = params.get("scene");
  const shot = params.get("shot");
  if (scene) target.scene_id = scene;
  if (shot) target.shot_id = shot;
  if (t !== undefined) target.t = t;
  return target;
}

// ---- Export bundles ------------------------------------------------------- //

/** A single self-describing bundle that carries a Director's full work for one
 *  book: canon snapshot + annotations + the collections that reference it. This
 *  is what "Export project" writes and "Import project" reads. */
export interface DirectorProjectExport {
  v: 1;
  kind: "kinora.director.project";
  book_id: string;
  exported_at: number;
  canon?: CanonGraph;
  annotations?: AnnotationExport;
  collections?: SmartCollection[];
}

export function buildProjectExport(
  bookId: string,
  parts: {
    canon?: CanonGraph;
    annotations?: AnnotationExport;
    collections?: SmartCollection[];
  },
  now: number = Date.now(),
): DirectorProjectExport {
  return {
    v: 1,
    kind: "kinora.director.project",
    book_id: bookId,
    exported_at: now,
    ...parts,
  };
}

/** Validate an imported blob is a DirectorProjectExport for inspection. */
export function isProjectExport(v: unknown): v is DirectorProjectExport {
  if (typeof v !== "object" || v === null) return false;
  const r = v as Record<string, unknown>;
  return r.v === 1 && r.kind === "kinora.director.project" && typeof r.book_id === "string";
}

/** Serialize an export to a pretty JSON string for a download / clipboard. */
export function serializeExport(bundle: object): string {
  return JSON.stringify(bundle, null, 2);
}

/** A suggested filename for an export download (no path, sanitized). */
export function exportFilename(prefix: string, bookTitle: string, ext = "json"): string {
  const slug = bookTitle
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48) || "book";
  return `${prefix}-${slug}.${ext}`;
}

// ---- Canon markdown export ------------------------------------------------ //

/** Render a canon graph to a human-readable markdown brief — uses the backend's
 *  own vault markdown when present, else synthesizes one from the entities +
 *  states so "Export canon as markdown" always produces something legible. */
export function canonToMarkdown(canon: CanonGraph): string {
  if (canon.markdown && canon.markdown.trim()) return canon.markdown;
  const lines: string[] = [`# Canon — ${canon.book_id}`, ""];
  if (canon.entities.length) {
    lines.push("## Entities", "");
    for (const e of canon.entities) {
      lines.push(`### ${e.name} (${e.type}) · v${e.version}`);
      if (e.aliases.length) lines.push(`*Also known as:* ${e.aliases.join(", ")}`);
      if (e.description) lines.push("", e.description);
      const appearance = e.appearance?.description;
      if (appearance) lines.push("", `**Appearance:** ${appearance}`);
      lines.push("");
    }
  }
  const active = canon.states.filter((s) => s.active);
  const retired = canon.states.filter((s) => !s.active);
  if (active.length) {
    lines.push("## Continuity (active)", "");
    for (const s of active) {
      lines.push(`- **${s.subject_entity_key}** ${s.predicate} → ${s.object_value} (from beat ${s.valid_from_beat})`);
    }
    lines.push("");
  }
  if (retired.length) {
    lines.push("## Continuity (retired — the story has since forgotten)", "");
    for (const s of retired) {
      lines.push(
        `- ~~**${s.subject_entity_key}** ${s.predicate} → ${s.object_value}~~ (beats ${s.valid_from_beat}–${s.valid_to_beat})`,
      );
    }
    lines.push("");
  }
  return lines.join("\n");
}
