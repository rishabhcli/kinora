import {
  type CanonEntityResponse,
  type CanonResponse,
  changesToRestore,
  dependentShotIds,
  type EditResult,
  ENTITY_GROUPS,
  queryKeys,
  type ShotResponse,
} from "@kinora/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import type { ShotUpdateMap } from "../../director/shots";
import { NATIVE_TOP_INSET, useNativeShell } from "../../../hooks/useNativeShell";
import { api } from "../../../lib/api";
import { CanonContinuitySection } from "./CanonContinuitySection";
import { CanonEntityCard } from "./CanonEntityCard";
import { DependentShotsStrip } from "./DependentShotsStrip";

interface CanonEditorPanelProps {
  bookId: string;
  shots: ShotResponse[] | undefined;
  /** The shared per-shot render map (rendering → ready) the Director timeline
   *  also reads — so the canon strip and the timeline stay in lockstep. */
  shotUpdates: ShotUpdateMap;
  lastEdit: EditResult | null;
  onEditApplied: (result: EditResult) => void;
  onClose: () => void;
}

interface EditVars {
  entityKey: string;
  changes: Record<string, unknown>;
  /** The entity *before* this edit — captured so the edit can be undone. */
  prior?: CanonEntityResponse;
}

/** Apply an edit to an entity locally for an instant, flicker-free update (§5.4),
 *  reconciled by the canon refetch. Bumps the version + the changed fields;
 *  rebuilds the appearance from the original refs (which carry the display URL). */
function applyOptimistic(entity: CanonEntityResponse, changes: Record<string, unknown>): CanonEntityResponse {
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

function SectionHeader({ label, blurb, count }: { label: string; blurb: string; count: number }) {
  return (
    <div className="flex items-baseline justify-between px-0.5">
      <div>
        <span className="text-[12px] font-semibold uppercase tracking-[0.16em] text-white/55">{label}</span>
        <span className="ml-2 text-[11px] text-white/30">{blurb}</span>
      </div>
      <span className="font-mono text-[11px] text-white/35">{count}</span>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-2.5">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="shimmer skeleton h-12 rounded-xl"
          style={{ "--shimmer-delay": `${i * 120}ms` } as React.CSSProperties}
        />
      ))}
    </div>
  );
}

/**
 * The §5.4 canon editor: the §8 memory graph rendered inspectable and editable as
 * a right-side slide-over. Entities are grouped (Characters · Locations · Props ·
 * Style) with the versioned continuity facts (§8.5) below; editing one and saving
 * calls `canon_edit`, then the dependent shots it lists re-render in place while
 * everything else stays a cache hit (§8.7). Edits apply optimistically and can be
 * undone; the panel traps focus and closes on Escape.
 */
export function CanonEditorPanel({
  bookId,
  shots,
  shotUpdates,
  lastEdit,
  onEditApplied,
  onClose,
}: CanonEditorPanelProps) {
  const native = useNativeShell();
  const queryClient = useQueryClient();
  const panelRef = useRef<HTMLElement | null>(null);
  const [mounted, setMounted] = useState(false);
  const [filter, setFilter] = useState("");
  const [vaultOpen, setVaultOpen] = useState(false);
  const [undoSnapshot, setUndoSnapshot] = useState<CanonEntityResponse | null>(null);

  // Escape to close + focus management: focus the panel on open, restore on close.
  useEffect(() => {
    setMounted(true);
    const restoreTo = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      restoreTo?.focus?.();
    };
  }, [onClose]);

  // A simple focus trap — Tab cycles within the slide-over (§a11y).
  const onPanelKeyDown = (e: ReactKeyboardEvent<HTMLElement>): void => {
    if (e.key !== "Tab" || !panelRef.current) return;
    const nodes = panelRef.current.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (nodes.length === 0) return;
    const first = nodes[0]!;
    const last = nodes[nodes.length - 1]!;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const { data: canon, isLoading, error } = useQuery({
    queryKey: queryKeys.canon(bookId),
    queryFn: async () => {
      const { data, error: err } = await api.GET("/api/books/{book_id}/canon", {
        params: { path: { book_id: bookId } },
      });
      if (err || !data) throw new Error("failed to load canon");
      return data;
    },
  });

  const mutation = useMutation({
    mutationFn: async (vars: EditVars) => {
      const { data, error: err } = await api.POST("/api/books/{book_id}/canon_edit", {
        params: { path: { book_id: bookId } },
        body: { entity_key: vars.entityKey, changes: vars.changes },
      });
      if (err || !data) throw new Error("canon edit failed");
      return data;
    },
    onMutate: async (vars: EditVars) => {
      await queryClient.cancelQueries({ queryKey: queryKeys.canon(bookId) });
      const previous = queryClient.getQueryData<CanonResponse>(queryKeys.canon(bookId));
      if (previous && Object.keys(vars.changes).length > 0) {
        queryClient.setQueryData<CanonResponse>(queryKeys.canon(bookId), {
          ...previous,
          entities: (previous.entities ?? []).map((e) =>
            e.id === vars.entityKey ? applyOptimistic(e, vars.changes) : e,
          ),
        });
      }
      return { previous };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.previous) queryClient.setQueryData(queryKeys.canon(bookId), ctx.previous);
    },
    onSuccess: (data, vars) => {
      setUndoSnapshot(vars.prior ?? null);
      const entity = canon?.entities?.find((e) => e.id === data.entity_key);
      onEditApplied({
        entityKey: data.entity_key,
        entityName: entity?.name ?? data.entity_key,
        entityType: entity?.type ?? "",
        version: data.version,
        affectedShotIds: data.affected_shot_ids ?? [],
        skipped: data.skipped_shots ?? 0,
        at: Date.now(),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.canon(bookId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.shots(bookId) });
    },
  });

  const save = (entity: CanonEntityResponse, changes: Record<string, unknown>): void => {
    mutation.mutate({ entityKey: entity.id, changes, prior: entity });
  };
  const undo = (): void => {
    if (!undoSnapshot) return;
    const prior = undoSnapshot;
    setUndoSnapshot(null);
    mutation.mutate({ entityKey: prior.id, changes: changesToRestore(prior) });
  };

  const entities = canon?.entities ?? [];
  const needle = filter.trim().toLowerCase();
  const visible = useMemo(
    () =>
      needle
        ? entities.filter(
            (e) =>
              e.name.toLowerCase().includes(needle) ||
              (e.aliases ?? []).some((a) => a.toLowerCase().includes(needle)),
          )
        : entities,
    [entities, needle],
  );
  const groups = ENTITY_GROUPS.map((g) => ({
    ...g,
    items: visible.filter((e) => e.type === g.type),
  })).filter((g) => g.items.length > 0);
  const other = visible.filter((e) => !ENTITY_GROUPS.some((g) => g.type === e.type));
  const states = needle ? [] : (canon?.states ?? []);

  const renderCard = (entity: CanonEntityResponse) => (
    <CanonEntityCard
      key={`${entity.id}:${entity.version}`}
      entity={entity}
      dependentCount={dependentShotIds(shots, entity.id).length}
      saving={mutation.isPending && mutation.variables?.entityKey === entity.id}
      onSave={(_entityKey, changes) => save(entity, changes)}
      onForceRerender={() => mutation.mutate({ entityKey: entity.id, changes: {}, prior: entity })}
    />
  );

  const topInset = native ? NATIVE_TOP_INSET : 0;

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-walnut-deep/30 backdrop-blur-[1px] transition-opacity duration-300"
        style={{ opacity: mounted ? 1 : 0 }}
        onClick={onClose}
        aria-hidden
      />
      <aside
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Canon editor"
        tabIndex={-1}
        onKeyDown={onPanelKeyDown}
        className="glass-strong no-drag fixed inset-y-0 right-0 z-50 flex w-[440px] max-w-[92vw] flex-col text-white outline-none transition-transform duration-300 ease-out"
        style={{ paddingTop: topInset, transform: mounted ? "translateX(0)" : "translateX(100%)" }}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-3 px-4 pb-3 pt-4">
          <div className="min-w-0">
            <h2 className="font-display text-[17px] font-semibold leading-tight">Canon</h2>
            <p className="mt-0.5 text-[11.5px] leading-snug text-white/45">
              The story bible — versioned. A re-read is free; an edit re-renders only the shots that cite it.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close canon editor"
            className="toolbar-btn shrink-0"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
              <path d="m6 6 12 12M18 6 6 18" />
            </svg>
          </button>
        </div>

        {/* Result banner — the surgical-regen outcome of the last edit. */}
        {lastEdit && (
          <div className="mx-4 mb-3 rounded-xl border border-ember-glow/30 bg-ember/10 px-3 py-2.5">
            <div className="flex items-start justify-between gap-2">
              <p className="text-[12px] text-white/80">
                Saved <span className="font-semibold text-white">{lastEdit.entityName}</span>{" "}
                <span className="font-mono text-[11px] text-ember-glow">v{lastEdit.version}</span>.{" "}
                {lastEdit.affectedShotIds.length === 0 ? (
                  <span className="text-white/55">No dependent shots — nothing to re-render.</span>
                ) : (
                  <>
                    <span className="font-semibold text-white">{lastEdit.affectedShotIds.length}</span> shot
                    {lastEdit.affectedShotIds.length === 1 ? "" : "s"} re-rendering
                    {lastEdit.skipped > 0 && (
                      <span className="text-white/55"> · {lastEdit.skipped} untouched (cache hit)</span>
                    )}
                  </>
                )}
              </p>
              {undoSnapshot && (
                <button
                  type="button"
                  onClick={undo}
                  disabled={mutation.isPending}
                  className="shrink-0 rounded-full bg-white/10 px-2.5 py-1 text-[11px] font-semibold text-white/85 transition hover:bg-white/20 disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
                >
                  Undo
                </button>
              )}
            </div>
            {lastEdit.affectedShotIds.length > 0 && (
              <div className="mt-2.5">
                <DependentShotsStrip shotIds={lastEdit.affectedShotIds} shots={shots} updates={shotUpdates} />
              </div>
            )}
          </div>
        )}

        {/* Filter */}
        {entities.length > 6 && (
          <div className="px-4 pb-2.5">
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter canon…"
              className="glass-input w-full rounded-full px-3.5 py-1.5 text-[12.5px]"
            />
          </div>
        )}

        {/* Body */}
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 pb-6 [scrollbar-width:thin]">
          {isLoading && <Skeleton />}
          {error && (
            <p className="rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-3 text-[12.5px] text-rose-200">
              Couldn't load the canon graph.
            </p>
          )}
          {!isLoading && !error && entities.length === 0 && (
            <p className="px-1 py-6 text-center text-[12.5px] text-white/40">
              No canon yet — it fills in as the book is read and shots are planned.
            </p>
          )}

          {groups.map((group) => (
            <section key={group.type} className="space-y-2.5">
              <SectionHeader label={group.label} blurb={group.blurb} count={group.items.length} />
              {group.items.map(renderCard)}
            </section>
          ))}

          {other.length > 0 && (
            <section className="space-y-2.5">
              <SectionHeader label="Other" blurb="" count={other.length} />
              {other.map(renderCard)}
            </section>
          )}

          <CanonContinuitySection states={states} />

          {mutation.isError && (
            <p className="rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-200">
              The edit didn't save. Try again.
            </p>
          )}

          {/* Inspectable markdown vault (§8.1) — the human-readable canon export. */}
          {canon?.markdown && (
            <section>
              <button
                type="button"
                onClick={() => setVaultOpen((v) => !v)}
                className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-white/40 transition hover:text-white/70"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" className={`transition-transform ${vaultOpen ? "rotate-90" : ""}`}>
                  <path d="m9 6 6 6-6 6" />
                </svg>
                Markdown vault
              </button>
              {vaultOpen && (
                <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-white/10 bg-black/30 p-3 font-mono text-[10.5px] leading-relaxed text-white/60 [scrollbar-width:thin]">
                  {canon.markdown}
                </pre>
              )}
            </section>
          )}
        </div>
      </aside>
    </>
  );
}
