// SmartCollectionEditor — create/save a smart collection from the CURRENT
// faceted query + sort. A smart collection is a saved (FacetQuery, sort)
// rule that re-evaluates against the live library, so "save current view as a
// collection" is the whole interaction. Editing reuses the same form.
import { useState } from "react";
import type { FacetQuery, SmartCollection, SortSpec } from "../../lib/api/collections";

interface SmartCollectionEditorProps {
  /** The query + sort to snapshot into the collection (the current view). */
  query: FacetQuery;
  sort: SortSpec[];
  /** Existing collection being edited, if any (else a fresh create). */
  editing?: SmartCollection | null;
  onSave: (collection: Omit<SmartCollection, "createdAt"> & { createdAt?: number }) => void;
  onCancel: () => void;
}

const ICONS = ["✦", "▶", "✓", "◉", "★", "❤", "⚑", "✎"];

function describeQuery(q: FacetQuery): string {
  const parts: string[] = [];
  if (q.text) parts.push(`“${q.text}”`);
  if (q.genres?.length) parts.push(q.genres.join("/"));
  if (q.eras?.length) parts.push(q.eras.join("/"));
  if (q.states?.length) parts.push(q.states.join("/"));
  if (q.liveOnly) parts.push("live");
  return parts.length ? parts.join(" · ") : "all books";
}

export default function SmartCollectionEditor({
  query,
  sort,
  editing,
  onSave,
  onCancel,
}: SmartCollectionEditorProps) {
  const [name, setName] = useState(editing?.name ?? "");
  const [icon, setIcon] = useState(editing?.icon ?? ICONS[0]);
  const [pinned, setPinned] = useState(editing?.pinned ?? false);

  const save = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    onSave({
      id: editing?.id ?? `user:${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      name: trimmed,
      query: editing ? editing.query : query,
      sort: editing ? editing.sort : sort,
      icon,
      pinned,
      createdAt: editing?.createdAt,
    });
  };

  return (
    <div
      className="rounded-xl p-3.5 mb-4"
      style={{ background: "rgba(212,164,78,0.06)", border: "1px solid rgba(212,164,78,0.22)" }}
    >
      <p className="text-[11px] font-medium text-kinora-text mb-2">
        {editing ? "Edit collection" : "Save this view as a smart collection"}
      </p>
      {!editing && <p className="text-[10px] text-kinora-muted mb-2">Rule: {describeQuery(query)}</p>}

      <div className="flex items-center gap-2 mb-2">
        <div className="flex gap-1">
          {ICONS.map((i) => (
            <button
              key={i}
              type="button"
              onClick={() => setIcon(i)}
              aria-pressed={i === icon}
              className="h-7 w-7 rounded-lg text-[13px] transition-colors"
              style={{
                background: i === icon ? "rgba(212,164,78,0.2)" : "rgba(255,255,255,0.04)",
                border: `1px solid ${i === icon ? "rgba(212,164,78,0.4)" : "rgba(255,255,255,0.08)"}`,
              }}
            >
              {i}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Collection name"
          autoFocus
          className="flex-1 rounded-lg px-2.5 py-1.5 text-[11.5px] text-kinora-text outline-none"
          style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)" }}
          onKeyDown={(e) => e.key === "Enter" && save()}
        />
        <label className="flex items-center gap-1.5 text-[10.5px] text-kinora-muted">
          <input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} />
          Pin
        </label>
        <button
          type="button"
          disabled={!name.trim()}
          onClick={save}
          className="rounded-lg px-3 py-1.5 text-[10.5px] font-semibold transition-all disabled:opacity-40"
          style={{ background: "linear-gradient(135deg, #d4a44e 0%, #c8923a 100%)", color: "#1a1408" }}
        >
          {editing ? "Save" : "Create"}
        </button>
        <button type="button" onClick={onCancel} className="text-[10.5px] text-kinora-muted hover:text-kinora-text transition-colors">
          Cancel
        </button>
      </div>
    </div>
  );
}
