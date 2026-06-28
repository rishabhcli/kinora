// LibraryWorkbench — the faceted library-organization surface. Combines the
// facet sidebar, the smart-collection rail, multi-key sorting, and a results
// grid. All filtering/sorting/collection logic comes from the pure
// `lib/api/collections` module; the collection store persists user collections.
//
// This is an alternate, power-user view of the library (the existing shelf view
// in LibraryPage stays the default). It is self-contained and reuses BookCard.
import { useCallback, useEffect, useMemo, useState } from "react";
import type { Book } from "../../data/books";
import BookCard from "../BookCard";
import FacetSidebar from "./FacetSidebar";
import CollectionRail from "./CollectionRail";
import SmartCollectionEditor from "./SmartCollectionEditor";
import {
  applyFacets,
  sortBySpecs,
  evaluateCollection,
  createCollectionStore,
  type CollectionStore,
  type FacetQuery,
  type SmartCollection,
  type SortField,
  type SortSpec,
} from "../../lib/api/collections";
import type { LibraryBook } from "../../lib/api/library";

interface LibraryWorkbenchProps {
  books: LibraryBook[];
  onOpenBook?: (book: Book) => void;
  /** Inject a store for tests; defaults to the persisted localStorage one. */
  store?: CollectionStore;
}

const SORT_OPTIONS: { field: SortField; label: string }[] = [
  { field: "recent", label: "Recently added" },
  { field: "title", label: "Title" },
  { field: "author", label: "Author" },
  { field: "progress", label: "Progress" },
  { field: "genre", label: "Genre" },
  { field: "era", label: "Era" },
];

function useCollectionStore(store: CollectionStore): SmartCollection[] {
  const [, setTick] = useState(0);
  useEffect(() => store.subscribe(() => setTick((n) => n + 1)), [store]);
  return store.list();
}

export default function LibraryWorkbench({ books, onOpenBook, store }: LibraryWorkbenchProps) {
  const collectionStore = useMemo(() => store ?? createCollectionStore(), [store]);
  const collections = useCollectionStore(collectionStore);

  const [query, setQuery] = useState<FacetQuery>({});
  const [sort, setSort] = useState<SortSpec>({ field: "recent", dir: "asc" });
  const [activeCollectionId, setActiveCollectionId] = useState<string | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [editing, setEditing] = useState<SmartCollection | null>(null);

  const activeCollection = collections.find((c) => c.id === activeCollectionId) ?? null;

  // Results: when a collection is active, evaluate it (its own rule + sort);
  // otherwise apply the live facet query + the chosen sort.
  const results = useMemo(() => {
    if (activeCollection) return evaluateCollection(books, activeCollection);
    const filtered = applyFacets(books, query);
    return sortBySpecs(filtered, [sort]);
  }, [activeCollection, books, query, sort]);

  const selectCollection = useCallback((c: SmartCollection | null) => {
    setActiveCollectionId(c?.id ?? null);
    setShowEditor(false);
    setEditing(null);
  }, []);

  const onSaveCollection = useCallback(
    (c: Omit<SmartCollection, "createdAt"> & { createdAt?: number }) => {
      const saved = collectionStore.upsert(c);
      setShowEditor(false);
      setEditing(null);
      setActiveCollectionId(saved.id);
    },
    [collectionStore],
  );

  return (
    <div className="flex flex-col gap-4">
      {/* Collection rail */}
      <div className="flex items-center gap-3">
        <div className="flex-1 min-w-0">
          <CollectionRail
            collections={collections}
            books={books}
            activeId={activeCollectionId}
            onSelect={selectCollection}
            onEdit={(c) => {
              setEditing(c);
              setShowEditor(true);
            }}
            onRemove={(id) => {
              collectionStore.remove(id);
              if (activeCollectionId === id) setActiveCollectionId(null);
            }}
          />
        </div>
        {!activeCollection && (
          <button
            type="button"
            onClick={() => {
              setEditing(null);
              setShowEditor((v) => !v);
            }}
            className="shrink-0 rounded-full px-3 py-1.5 text-[11px] font-medium transition-colors"
            style={{ background: "rgba(255,255,255,0.05)", color: "rgba(236,231,223,0.85)", border: "1px solid rgba(255,255,255,0.1)" }}
          >
            + Save view
          </button>
        )}
      </div>

      {showEditor && (
        <SmartCollectionEditor
          query={query}
          sort={[sort]}
          editing={editing}
          onSave={onSaveCollection}
          onCancel={() => {
            setShowEditor(false);
            setEditing(null);
          }}
        />
      )}

      <div className="flex gap-6">
        {/* Facet sidebar — hidden when a collection drives the view (its rule wins). */}
        {!activeCollection && <FacetSidebar books={books} query={query} onChange={setQuery} />}

        <div className="flex-1 min-w-0">
          {/* Search + sort bar */}
          <div className="flex flex-wrap items-center gap-3 mb-4">
            {!activeCollection && (
              <input
                type="search"
                value={query.text ?? ""}
                onChange={(e) => setQuery({ ...query, text: e.target.value })}
                placeholder="Search title, author, genre…"
                aria-label="Search the library"
                className="flex-1 min-w-[200px] rounded-xl px-3 py-2 text-[12px] text-kinora-text outline-none"
                style={{ background: "rgba(255,255,255,0.045)", border: "1px solid rgba(255,255,255,0.08)" }}
              />
            )}
            <label className="flex items-center gap-2 text-[11px] text-kinora-muted ml-auto">
              Sort
              <select
                value={sort.field}
                onChange={(e) => setSort({ ...sort, field: e.target.value as SortField })}
                disabled={Boolean(activeCollection)}
                aria-label="Sort field"
                className="rounded-xl px-3 py-2 text-[11px] text-kinora-text outline-none disabled:opacity-50"
                style={{ background: "rgba(255,255,255,0.045)", border: "1px solid rgba(255,255,255,0.08)" }}
              >
                {SORT_OPTIONS.map((o) => (
                  <option key={o.field} value={o.field} style={{ color: "#1a1408" }}>
                    {o.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                disabled={Boolean(activeCollection)}
                onClick={() => setSort({ ...sort, dir: sort.dir === "asc" ? "desc" : "asc" })}
                aria-label={`Sort direction ${sort.dir}`}
                className="rounded-xl px-2.5 py-2 text-[11px] text-kinora-text disabled:opacity-50"
                style={{ background: "rgba(255,255,255,0.045)", border: "1px solid rgba(255,255,255,0.08)" }}
              >
                {sort.dir === "asc" ? "↑" : "↓"}
              </button>
            </label>
          </div>

          <p className="text-[11px] text-kinora-muted mb-3">
            {results.length} book{results.length === 1 ? "" : "s"}
            {activeCollection ? ` in “${activeCollection.name}”` : ""}
          </p>

          {results.length === 0 ? (
            <div className="py-16 text-center rounded-2xl" style={{ background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.05)" }}>
              <p className="text-[12px] text-kinora-muted">No books match.</p>
            </div>
          ) : (
            <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))" }}>
              {results.map((b) => (
                <BookCard key={b.id} book={b} onOpen={onOpenBook} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
