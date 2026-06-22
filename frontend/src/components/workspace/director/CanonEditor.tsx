import { useEffect, useMemo, useState } from "react";

import { ApiError, books as booksApi } from "../../../api/client";
import type { CanonEntity, CanonGraph } from "../../../api/types";
import { Spinner } from "../../common/icons";

interface CanonEditorProps {
  bookId: string;
  canon: CanonGraph | null;
  onEdited: (affectedShotIds: string[]) => void;
}

const KIND_LABEL: Record<string, string> = {
  character: "Characters",
  location: "Locations",
  prop: "Props",
  style: "Style",
};

export function CanonEditor({ bookId, canon, onEdited }: CanonEditorProps) {
  const entities = useMemo(() => canon?.entities ?? [], [canon]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [refUrl, setRefUrl] = useState("");
  const [tokens, setTokens] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const grouped = useMemo(() => {
    const groups: Record<string, CanonEntity[]> = {};
    for (const e of entities) (groups[e.type] ??= []).push(e);
    return groups;
  }, [entities]);

  const selected = entities.find((e) => e.id === selectedId) ?? null;

  useEffect(() => {
    if (!selected) return;
    setDescription(selected.appearance?.description ?? selected.description ?? "");
    setRefUrl(selected.appearance?.reference_images?.[0]?.oss_url ?? "");
    setTokens(selected.style_tokens ?? {});
    setStatus(null);
    setError(null);
  }, [selectedId]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    if (!selected) return;
    setSaving(true);
    setError(null);
    setStatus(null);
    const changes: Record<string, unknown> = {};
    if (description !== (selected.appearance?.description ?? selected.description ?? "")) {
      changes.description = description;
    }
    if (refUrl && refUrl !== (selected.appearance?.reference_images?.[0]?.oss_url ?? "")) {
      changes.reference_image_url = refUrl;
    }
    if (selected.type === "style") changes.style_tokens = tokens;

    if (Object.keys(changes).length === 0) {
      setStatus("No changes to save.");
      setSaving(false);
      return;
    }
    try {
      const res = await booksApi.canonEdit(bookId, { entity_key: selected.id, changes });
      const affected = res.affected_shot_ids ?? [];
      onEdited(affected);
      setStatus(
        affected.length
          ? `Regenerating ${affected.length} dependent shot${affected.length === 1 ? "" : "s"}…`
          : "Saved. No dependent shots needed regenerating.",
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the edit.");
    } finally {
      setSaving(false);
    }
  };

  if (!canon) {
    return (
      <div className="flex items-center gap-2 text-sm text-kinora-muted">
        <Spinner className="h-4 w-4" /> Loading canon…
      </div>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-[200px_1fr]">
      <nav className="scrollbar-thin max-h-72 space-y-3 overflow-y-auto pr-1">
        {Object.entries(grouped).map(([kind, items]) => (
          <div key={kind}>
            <p className="mb-1 text-[0.65rem] font-semibold uppercase tracking-wider text-kinora-muted">
              {KIND_LABEL[kind] ?? kind}
            </p>
            <ul className="space-y-0.5">
              {items.map((e) => (
                <li key={e.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(e.id)}
                    className={`w-full truncate rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors ${
                      selectedId === e.id
                        ? "bg-kinora-glow/20 text-kinora-mist"
                        : "text-kinora-muted hover:bg-white/5 hover:text-kinora-mist"
                    }`}
                  >
                    {e.name}
                    {e.version ? (
                      <span className="ml-1 text-[0.65rem] text-kinora-muted/70">v{e.version}</span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
        {entities.length === 0 ? (
          <p className="text-sm text-kinora-muted">Canon is still being built.</p>
        ) : null}
      </nav>

      <div className="min-w-0">
        {selected ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <h4 className="text-sm font-semibold text-kinora-mist">{selected.name}</h4>
              <span className="rounded-full bg-white/10 px-2 py-0.5 text-[0.65rem] text-kinora-muted">
                {selected.type}
              </span>
            </div>

            {selected.appearance?.reference_images?.length ? (
              <div className="flex gap-2">
                {selected.appearance.reference_images.slice(0, 3).map((ref, i) => (
                  <img
                    key={i}
                    src={ref.oss_url}
                    alt=""
                    className="h-14 w-14 rounded-lg object-cover ring-1 ring-white/15"
                  />
                ))}
              </div>
            ) : null}

            <label className="block">
              <span className="mb-1 block text-xs font-medium text-kinora-muted">
                Appearance description
              </span>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="w-full resize-none rounded-lg border border-kinora-line bg-kinora-ink/60 px-3 py-2 text-sm text-kinora-mist outline-none focus:border-kinora-iris/70"
              />
            </label>

            <label className="block">
              <span className="mb-1 block text-xs font-medium text-kinora-muted">
                Swap a locked reference image (URL)
              </span>
              <input
                value={refUrl}
                onChange={(e) => setRefUrl(e.target.value)}
                placeholder="https://…/reference.png"
                className="w-full rounded-lg border border-kinora-line bg-kinora-ink/60 px-3 py-2 text-sm text-kinora-mist outline-none focus:border-kinora-iris/70"
              />
            </label>

            {selected.type === "style" ? (
              <div className="space-y-2">
                <span className="block text-xs font-medium text-kinora-muted">Style tokens</span>
                {Object.entries(tokens).map(([k, v]) => (
                  <div key={k} className="flex items-center gap-2">
                    <span className="w-24 shrink-0 truncate text-xs text-kinora-muted">{k}</span>
                    <input
                      value={v}
                      onChange={(e) => setTokens((t) => ({ ...t, [k]: e.target.value }))}
                      className="flex-1 rounded-lg border border-kinora-line bg-kinora-ink/60 px-2.5 py-1.5 text-sm text-kinora-mist outline-none focus:border-kinora-iris/70"
                    />
                  </div>
                ))}
              </div>
            ) : null}

            <div className="flex items-center gap-3 pt-1">
              <button
                type="button"
                onClick={save}
                disabled={saving}
                className="inline-flex items-center gap-2 rounded-full bg-[#6d28d9] px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-[#7c5cff] disabled:opacity-60"
              >
                {saving ? <Spinner className="h-4 w-4" /> : null}
                Save & regenerate dependents
              </button>
              {status ? <span className="text-xs text-kinora-iris">{status}</span> : null}
              {error ? <span className="text-xs text-kinora-danger">{error}</span> : null}
            </div>
            <p className="text-[0.7rem] leading-relaxed text-kinora-muted/80">
              Only shots whose reference set includes this entity are re-rendered — surgical, not a
              full re-render (kinora.md §8.7).
            </p>
          </div>
        ) : (
          <p className="text-sm text-kinora-muted">
            Select an entity to inspect and edit its canon — appearance, references, or style.
          </p>
        )}
      </div>
    </div>
  );
}
