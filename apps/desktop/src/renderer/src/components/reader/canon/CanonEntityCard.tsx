import {
  type CanonEntityResponse,
  buildChanges,
  draftFromEntity,
  type EntityDraft,
  validateCanonDraft,
} from "@kinora/core";
import { type KeyboardEvent, useState } from "react";

import { ReferenceImageGrid } from "./ReferenceImageGrid";
import { ReferenceLightbox } from "./ReferenceLightbox";
import { StyleTokensEditor } from "./StyleTokensEditor";

interface CanonEntityCardProps {
  entity: CanonEntityResponse;
  /** How many shots cite this entity — the surgical-regen blast radius (§8.7). */
  dependentCount: number;
  /** True while this entity's own edit is in flight. */
  saving: boolean;
  defaultOpen?: boolean;
  onSave: (entityKey: string, changes: Record<string, unknown>) => void;
  /** Force a fresh take of all dependent shots without a field change (§8.7). */
  onForceRerender?: (entityKey: string) => void;
}

const TYPE_TINT: Record<string, string> = {
  character: "bg-ember/20 text-ember-glow",
  location: "bg-emerald-400/15 text-emerald-200",
  prop: "bg-sky-400/15 text-sky-200",
  style: "bg-violet-400/15 text-violet-200",
};

function FieldLabel({ children }: { children: string }) {
  return (
    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-white/40">
      {children}
    </p>
  );
}

/**
 * One canon entity rendered inspectable + editable (§5.4 canon editor). Edit the
 * name, aliases, appearance description, locked reference set, or — for a Style
 * node — its palette/lens/art-direction. On save it diffs to a minimal
 * `canon_edit` `changes` map; the version chip and the "N shots" count make the
 * surgical blast radius legible before you commit. Guardrails (§8.1) block an
 * empty name or unlocking the whole reference set; ⌘/Ctrl+Enter saves.
 */
export function CanonEntityCard({
  entity,
  dependentCount,
  saving,
  defaultOpen = false,
  onSave,
  onForceRerender,
}: CanonEntityCardProps) {
  const [draft, setDraft] = useState<EntityDraft>(() => draftFromEntity(entity));
  const [expanded, setExpanded] = useState(defaultOpen);
  const [lightbox, setLightbox] = useState<number | null>(null);

  const changes = buildChanges(entity, draft);
  const dirty = Object.keys(changes).length > 0;
  const errors = validateCanonDraft(entity, draft);
  const canSave = dirty && errors.length === 0 && !saving;
  const lightboxRef = lightbox !== null ? draft.references[lightbox] : undefined;
  const set = (patch: Partial<EntityDraft>): void => setDraft((d) => ({ ...d, ...patch }));

  const toggleLock = (index: number): void =>
    setDraft((d) => ({
      ...d,
      references: d.references.map((r, i) => (i === index ? { ...r, locked: !r.locked } : r)),
    }));

  const onBodyKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && canSave) {
      e.preventDefault();
      onSave(entity.id, changes);
    }
  };

  const showAppearance =
    entity.type === "character" ||
    entity.type === "location" ||
    entity.type === "prop" ||
    draft.references.length > 0;
  const voiceId =
    entity.voice && typeof entity.voice["cosyvoice_voice_id"] === "string"
      ? (entity.voice["cosyvoice_voice_id"] as string)
      : null;

  return (
    <>
      <div className={`overflow-hidden rounded-xl border transition ${dirty ? "border-ember-glow/50" : "border-white/10"} bg-white/[0.03]`}>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition hover:bg-white/[0.04] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
        >
          <svg
            width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"
            className={`shrink-0 text-white/40 transition-transform ${expanded ? "rotate-90" : ""}`}
          >
            <path d="m9 6 6 6-6 6" />
          </svg>
          <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium text-white">{draft.name || entity.name}</span>
          {dirty && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-ember-glow" aria-label="Unsaved changes" />}
          <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide ${TYPE_TINT[entity.type] ?? "bg-white/10 text-white/60"}`}>
            {entity.type}
          </span>
          <span className="shrink-0 rounded-full bg-white/8 px-1.5 py-0.5 font-mono text-[10px] text-white/55" title="Canon version">
            v{entity.version}
          </span>
        </button>

        {expanded && (
          <div className="space-y-3.5 border-t border-white/8 px-3 py-3" onKeyDown={onBodyKeyDown}>
            <p className="text-[11px] text-white/45">
              {dependentCount === 0
                ? "No shots cite this yet — an edit re-renders nothing."
                : `${dependentCount} shot${dependentCount === 1 ? "" : "s"} cite this — an edit re-renders only ${dependentCount === 1 ? "it" : "those"}.`}
            </p>

            <div>
              <FieldLabel>Name</FieldLabel>
              <input
                value={draft.name}
                onChange={(e) => set({ name: e.target.value })}
                className="glass-input w-full rounded-lg px-3 py-2 text-[13px]"
              />
            </div>

            <div>
              <FieldLabel>Aliases</FieldLabel>
              <input
                value={draft.aliasesText}
                onChange={(e) => set({ aliasesText: e.target.value })}
                placeholder="comma, separated"
                className="glass-input w-full rounded-lg px-3 py-2 text-[13px]"
              />
            </div>

            <div>
              <FieldLabel>Description</FieldLabel>
              <textarea
                value={draft.description}
                onChange={(e) => set({ description: e.target.value })}
                rows={2}
                className="glass-input w-full resize-y rounded-lg px-3 py-2 text-[13px] leading-snug"
              />
            </div>

            {showAppearance && (
              <div>
                <FieldLabel>Appearance</FieldLabel>
                <textarea
                  value={draft.appearanceDescription}
                  onChange={(e) => set({ appearanceDescription: e.target.value })}
                  placeholder="the canonical look — features, wardrobe, palette"
                  rows={2}
                  className="glass-input mb-2.5 w-full resize-y rounded-lg px-3 py-2 text-[13px] leading-snug"
                />
                <ReferenceImageGrid
                  references={draft.references}
                  onToggleLock={toggleLock}
                  onView={(i) => setLightbox(i)}
                />
              </div>
            )}

            {entity.type === "style" && (
              <StyleTokensEditor
                palette={draft.palette}
                lens={draft.lens}
                artDirection={draft.artDirection}
                onChange={(next) =>
                  set({
                    ...(next.palette !== undefined ? { palette: next.palette } : {}),
                    ...(next.lens !== undefined ? { lens: next.lens } : {}),
                    ...(next.artDirection !== undefined ? { artDirection: next.artDirection } : {}),
                  })
                }
              />
            )}

            {voiceId && (
              <p className="text-[11px] text-white/40">
                Voice <span className="font-mono text-white/55">{voiceId}</span> — edited from the audio tools.
              </p>
            )}

            {errors.length > 0 && (
              <ul className="space-y-1">
                {errors.map((err) => (
                  <li key={err} className="flex items-start gap-1.5 text-[11px] text-amber-300">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 shrink-0">
                      <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
                    </svg>
                    {err}
                  </li>
                ))}
              </ul>
            )}

            <div className="flex items-center justify-end gap-2 pt-0.5">
              {dependentCount > 0 && onForceRerender && (
                <button
                  type="button"
                  onClick={() => onForceRerender(entity.id)}
                  disabled={saving}
                  title="Re-render all dependent shots with a fresh take (no field change)"
                  className="mr-auto rounded-full px-3 py-1.5 text-[12px] text-white/55 transition hover:text-white disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
                >
                  Re-render {dependentCount}
                </button>
              )}
              <button
                type="button"
                onClick={() => setDraft(draftFromEntity(entity))}
                disabled={!dirty || saving}
                className="rounded-full px-3 py-1.5 text-[12px] text-white/60 transition hover:text-white disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
              >
                Reset
              </button>
              <button
                type="button"
                onClick={() => onSave(entity.id, changes)}
                disabled={!canSave}
                title={errors[0] ?? "Save & re-render dependent shots (⌘↵)"}
                className="flex items-center gap-1.5 rounded-full bg-white/90 px-4 py-1.5 text-[12px] font-semibold text-walnut-deep transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
              >
                {saving && (
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-walnut-deep/30 border-t-walnut-deep motion-reduce:animate-none" />
                )}
                {saving ? "Saving" : "Save & re-render"}
              </button>
            </div>
          </div>
        )}
      </div>

      {lightboxRef && <ReferenceLightbox reference={lightboxRef} onClose={() => setLightbox(null)} />}
    </>
  );
}
