// CollectionRail — the horizontal rail of smart collections (built-ins + the
// user's saved ones, in rail order). Selecting one applies its rule as the
// active view; user collections can be edited or removed. Each chip shows its
// live member count (re-evaluated against the current library).
import {
  evaluateCollection,
  isBuiltinCollection,
  type SmartCollection,
} from "../../lib/api/collections";
import type { LibraryBook } from "../../lib/api/library";

interface CollectionRailProps {
  collections: SmartCollection[];
  books: LibraryBook[];
  activeId: string | null;
  onSelect: (collection: SmartCollection | null) => void;
  onEdit: (collection: SmartCollection) => void;
  onRemove: (id: string) => void;
}

export default function CollectionRail({
  collections,
  books,
  activeId,
  onSelect,
  onEdit,
  onRemove,
}: CollectionRailProps) {
  return (
    <div className="flex items-center gap-2 overflow-x-auto hide-scrollbar pb-1" role="tablist" aria-label="Smart collections">
      <button
        type="button"
        role="tab"
        aria-selected={activeId === null}
        onClick={() => onSelect(null)}
        className="shrink-0 rounded-full px-3 py-1.5 text-[11px] font-medium transition-all"
        style={{
          background: activeId === null ? "linear-gradient(135deg, #d4a44e 0%, #c8923a 100%)" : "rgba(255,255,255,0.04)",
          color: activeId === null ? "#1a1408" : "rgba(236,231,223,0.82)",
          border: `1px solid ${activeId === null ? "rgba(212,164,78,0.4)" : "rgba(255,255,255,0.08)"}`,
        }}
      >
        All books
      </button>

      {collections.map((c) => {
        const count = evaluateCollection(books, c).length;
        const active = c.id === activeId;
        const builtin = isBuiltinCollection(c);
        return (
          <div key={c.id} className="group/coll relative shrink-0">
            <button
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onSelect(c)}
              className="rounded-full pl-2.5 pr-3 py-1.5 text-[11px] font-medium transition-all flex items-center gap-1.5"
              style={{
                background: active ? "rgba(212,164,78,0.18)" : "rgba(255,255,255,0.04)",
                color: active ? "rgba(236,231,223,0.98)" : "rgba(236,231,223,0.82)",
                border: `1px solid ${active ? "rgba(212,164,78,0.35)" : "rgba(255,255,255,0.08)"}`,
              }}
            >
              {c.icon && <span aria-hidden>{c.icon}</span>}
              <span>{c.name}</span>
              <span className="text-[9.5px] text-kinora-muted tabular-nums">{count}</span>
            </button>
            {!builtin && (
              <div className="absolute -top-1.5 right-0 hidden group-hover/coll:flex gap-0.5">
                <button
                  type="button"
                  aria-label={`Edit ${c.name}`}
                  onClick={() => onEdit(c)}
                  className="h-4 w-4 rounded-full text-[8px] flex items-center justify-center"
                  style={{ background: "rgba(255,255,255,0.12)", color: "rgba(236,231,223,0.9)" }}
                >
                  ✎
                </button>
                <button
                  type="button"
                  aria-label={`Remove ${c.name}`}
                  onClick={() => onRemove(c.id)}
                  className="h-4 w-4 rounded-full text-[8px] flex items-center justify-center"
                  style={{ background: "rgba(248,113,113,0.25)", color: "#f87171" }}
                >
                  ✕
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
