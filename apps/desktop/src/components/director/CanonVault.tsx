// CanonVault — the §8.1 canon viewer + §5.4/§8.7 entity editor. Lists the book's
// canon entities (current versions) and continuity facts (active + retired), and
// lets the Director edit an entity's description/appearance. Saving POSTs
// `POST /api/books/{id}/canon_edit`, which writes a new entity version and
// SURGICALLY regenerates only the dependent shots (everything else stays a
// cache hit). Before saving, the editor previews the blast radius from the
// loaded shot set (how many shots reference the entity).
import { useCallback, useMemo, useState } from "react";
import { ApiError } from "../../lib/api";
import {
  director,
  canonEditBlastRadius,
  type CanonEntity,
  type CanonGraph,
  type CanonState,
  type DirectorShot,
} from "../../lib/api/director";
import { canonToMarkdown } from "../../lib/api/sharing";

interface CanonVaultProps {
  bookId: string;
  canon: CanonGraph;
  shots: DirectorShot[];
  /** Re-fetch the canon after an edit lands a new version. */
  onEdited?: (affectedShotIds: string[]) => void;
}

interface EditDraft {
  name: string;
  description: string;
  appearance: string;
}

function draftFromEntity(e: CanonEntity): EditDraft {
  return {
    name: e.name,
    description: e.description ?? "",
    appearance: e.appearance?.description ?? "",
  };
}

function StateRow({ state }: { state: CanonState }) {
  return (
    <li
      className="rounded-lg px-2.5 py-1.5 text-[11px]"
      style={{ background: "rgba(255,255,255,0.025)", border: "1px solid rgba(255,255,255,0.05)", opacity: state.active ? 1 : 0.55 }}
    >
      <span className={state.active ? "text-kinora-text" : "text-kinora-muted line-through"}>
        <span className="font-medium">{state.subject_entity_key}</span> {state.predicate} → {state.object_value}
      </span>
      <span className="text-[9.5px] text-kinora-muted ml-2">
        {state.active ? `from beat ${state.valid_from_beat}` : `retired @ ${state.valid_to_beat}`}
      </span>
    </li>
  );
}

export default function CanonVault({ bookId, canon, shots, onEdited }: CanonVaultProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<EditDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<string | null>(null);
  const [showMarkdown, setShowMarkdown] = useState(false);

  const startEdit = useCallback((e: CanonEntity) => {
    setEditing(e.id);
    setDraft(draftFromEntity(e));
    setError(null);
    setLastResult(null);
  }, []);

  const save = useCallback(
    async (entity: CanonEntity) => {
      if (!draft || busy) return;
      setBusy(true);
      setError(null);
      try {
        const changes: Record<string, unknown> = {};
        if (draft.name !== entity.name) changes.name = draft.name;
        if (draft.description !== (entity.description ?? "")) changes.description = draft.description;
        if (draft.appearance !== (entity.appearance?.description ?? "")) {
          // The backend merges into appearance; preserve existing reference image
          // KEYS (the durable ids) so a description tweak doesn't drop the locks.
          const refKeys = (entity.appearance?.reference_images ?? [])
            .map((r) => r.oss_key)
            .filter((k): k is string => Boolean(k));
          changes.appearance = {
            description: draft.appearance,
            ...(refKeys.length ? { reference_image_keys: refKeys } : {}),
          };
        }
        if (Object.keys(changes).length === 0) {
          setEditing(null);
          setDraft(null);
          return;
        }
        const res = await director.canonEdit(bookId, { entity_key: entity.id, changes });
        setLastResult(
          `Saved → v${res.version}. ${res.affected_shot_ids.length} shot${res.affected_shot_ids.length === 1 ? "" : "s"} re-rendering, ${res.skipped_shots} cache hits.`,
        );
        setEditing(null);
        setDraft(null);
        onEdited?.(res.affected_shot_ids);
      } catch (e) {
        setError(
          e instanceof ApiError
            ? e.status === 404
              ? "That entity no longer exists."
              : `Save failed (${e.status}).`
            : "Save failed — please try again.",
        );
      } finally {
        setBusy(false);
      }
    },
    [draft, busy, bookId, onEdited],
  );

  const markdown = useMemo(() => canonToMarkdown(canon), [canon]);
  const activeStates = canon.states.filter((s) => s.active);
  const retiredStates = canon.states.filter((s) => !s.active);

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-kinora-muted">
          {canon.entities.length} entities · {activeStates.length} active facts · {retiredStates.length} retired
        </p>
        <button
          type="button"
          onClick={() => setShowMarkdown((v) => !v)}
          className="text-[10.5px] font-medium text-kinora-muted hover:text-kinora-text transition-colors"
        >
          {showMarkdown ? "Hide" : "View"} vault markdown
        </button>
      </div>

      {lastResult && (
        <p className="rounded-lg px-3 py-2 text-[11px]" style={{ background: "rgba(52,211,153,0.08)", border: "1px solid rgba(52,211,153,0.2)", color: "rgba(236,231,223,0.95)" }}>
          {lastResult}
        </p>
      )}
      {error && (
        <p className="text-[11px]" style={{ color: "#f87171" }} role="alert">
          {error}
        </p>
      )}

      {showMarkdown && (
        <pre
          className="rounded-xl p-3 text-[10.5px] text-kinora-text/90 whitespace-pre-wrap overflow-x-auto hide-scrollbar"
          style={{ background: "rgba(0,0,0,0.25)", border: "1px solid rgba(255,255,255,0.06)", maxHeight: 240 }}
        >
          {markdown}
        </pre>
      )}

      {/* Entities */}
      <div className="grid gap-3 sm:grid-cols-2">
        {canon.entities.map((e) => {
          const blast = canonEditBlastRadius(shots, e.id);
          const isEditing = editing === e.id;
          const ref = e.appearance?.reference_images?.[0];
          return (
            <div
              key={e.id}
              className="rounded-xl p-3"
              style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${isEditing ? "rgba(212,164,78,0.4)" : "rgba(255,255,255,0.07)"}` }}
            >
              <div className="flex items-start gap-3">
                {ref && (
                  <img
                    src={ref.oss_url.replace("://minio:9000/", "://localhost:9000/").split("?")[0]}
                    alt={e.name}
                    className="h-12 w-12 rounded-lg object-cover shrink-0"
                    style={{ border: "1px solid rgba(255,255,255,0.1)" }}
                  />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <h4 className="text-[12.5px] font-semibold text-kinora-text truncate">{e.name}</h4>
                    <span className="text-[9px] text-kinora-muted">v{e.version}</span>
                  </div>
                  <p className="text-[10px] text-kinora-muted">
                    {e.type}
                    {blast > 0 ? ` · ${blast} dependent shot${blast === 1 ? "" : "s"}` : ""}
                  </p>
                </div>
              </div>

              {isEditing && draft ? (
                <div className="mt-3 flex flex-col gap-2">
                  <input
                    value={draft.name}
                    onChange={(ev) => setDraft({ ...draft, name: ev.target.value })}
                    placeholder="Name"
                    className="rounded-lg px-2.5 py-1.5 text-[11.5px] text-kinora-text outline-none"
                    style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)" }}
                  />
                  <textarea
                    value={draft.description}
                    onChange={(ev) => setDraft({ ...draft, description: ev.target.value })}
                    rows={2}
                    placeholder="Description"
                    className="resize-none rounded-lg px-2.5 py-1.5 text-[11.5px] text-kinora-text outline-none"
                    style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)" }}
                  />
                  <textarea
                    value={draft.appearance}
                    onChange={(ev) => setDraft({ ...draft, appearance: ev.target.value })}
                    rows={2}
                    placeholder="Appearance (drives the look)"
                    className="resize-none rounded-lg px-2.5 py-1.5 text-[11.5px] text-kinora-text outline-none"
                    style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)" }}
                  />
                  {blast > 0 && (
                    <p className="text-[10px] text-kinora-muted">
                      Saving re-renders {blast} dependent shot{blast === 1 ? "" : "s"} (the rest stay cache hits).
                    </p>
                  )}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void save(e)}
                      className="rounded-lg px-3 py-1.5 text-[10.5px] font-semibold transition-all disabled:opacity-40"
                      style={{ background: "linear-gradient(135deg, #d4a44e 0%, #c8923a 100%)", color: "#1a1408" }}
                    >
                      {busy ? "Saving…" : "Save & re-render"}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setEditing(null);
                        setDraft(null);
                      }}
                      className="text-[10.5px] text-kinora-muted hover:text-kinora-text transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  {e.description && <p className="mt-2 text-[11px] text-kinora-text/85 line-clamp-3">{e.description}</p>}
                  {e.appearance?.description && (
                    <p className="mt-1 text-[10.5px] text-kinora-muted line-clamp-2">Look: {e.appearance.description}</p>
                  )}
                  <button
                    type="button"
                    onClick={() => startEdit(e)}
                    className="mt-2 text-[10.5px] font-medium text-kinora-muted hover:text-kinora-text transition-colors"
                  >
                    Edit canon →
                  </button>
                </>
              )}
            </div>
          );
        })}
      </div>

      {/* Continuity facts */}
      {(activeStates.length > 0 || retiredStates.length > 0) && (
        <div>
          <p className="text-[11px] font-medium text-kinora-text mb-2">Continuity facts</p>
          <ul className="flex flex-col gap-1.5">
            {activeStates.map((s) => (
              <StateRow key={s.id} state={s} />
            ))}
            {retiredStates.map((s) => (
              <StateRow key={s.id} state={s} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
