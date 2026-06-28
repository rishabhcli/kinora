import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import LibraryWorkbench from "./LibraryWorkbench";
import { createCollectionStore, type KeyValueStore } from "../../lib/api/collections";
import type { LibraryBook } from "../../lib/api/library";

function book(over: Partial<LibraryBook> = {}): LibraryBook {
  return {
    id: over.id ?? "id",
    title: over.title ?? "Title",
    author: over.author ?? "Author",
    progress: over.progress ?? 0,
    coverColor: "#000",
    coverGradient: "g",
    coverImage: "",
    textColor: "#fff",
    spineColor: "#000",
    live: over.live,
    genre: over.genre,
    era: over.era,
  };
}

const memStore = (): KeyValueStore => {
  const m = new Map<string, string>();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
};

// A facet button is a toggle (aria-pressed) whose label starts with the value;
// this disambiguates it from BookCard buttons (which also contain the genre in
// their accessible name).
function facetButton(label: string): HTMLElement {
  const match = screen
    .getAllByRole("button")
    .find((b) => b.getAttribute("aria-pressed") !== null && (b.textContent ?? "").startsWith(label));
  if (!match) throw new Error(`facet button not found: ${label}`);
  return match;
}

const lib: LibraryBook[] = [
  book({ id: "a", title: "Moby Dick", author: "Melville", genre: "Adventure", progress: 40, live: true }),
  book({ id: "b", title: "Pride and Prejudice", author: "Austen", genre: "Romance", progress: 100 }),
  book({ id: "c", title: "Neuromancer", author: "Gibson", genre: "Science Fiction", progress: 0, live: true }),
];

describe("LibraryWorkbench", () => {
  it("renders all books and the count by default", () => {
    render(<LibraryWorkbench books={lib} store={createCollectionStore(memStore())} />);
    expect(screen.getByText("3 books")).toBeInTheDocument();
    expect(screen.getByText("Moby Dick")).toBeInTheDocument();
  });

  it("filters by a genre facet", () => {
    render(<LibraryWorkbench books={lib} store={createCollectionStore(memStore())} />);
    fireEvent.click(facetButton("Science Fiction"));
    expect(screen.getByText("1 book")).toBeInTheDocument();
    expect(screen.getByText("Neuromancer")).toBeInTheDocument();
    expect(screen.queryByText("Moby Dick")).not.toBeInTheDocument();
  });

  it("applies a built-in smart collection from the rail", () => {
    render(<LibraryWorkbench books={lib} store={createCollectionStore(memStore())} />);
    // "Continue Reading" surfaces only in-progress books (Moby Dick @ 40%).
    fireEvent.click(screen.getByRole("tab", { name: /Continue Reading/i }));
    expect(screen.getByText(/in “Continue Reading”/)).toBeInTheDocument();
    expect(screen.getByText("Moby Dick")).toBeInTheDocument();
    expect(screen.queryByText("Neuromancer")).not.toBeInTheDocument();
  });

  it("saves the current view as a smart collection", () => {
    const store = createCollectionStore(memStore());
    render(<LibraryWorkbench books={lib} store={store} />);
    fireEvent.click(facetButton("Science Fiction"));
    fireEvent.click(screen.getByRole("button", { name: /save view/i }));
    fireEvent.change(screen.getByPlaceholderText(/collection name/i), { target: { value: "My SF" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(store.userCollections().map((c) => c.name)).toContain("My SF");
    // it becomes the active collection
    expect(screen.getByText(/in “My SF”/)).toBeInTheDocument();
  });

  it("searches by text", () => {
    render(<LibraryWorkbench books={lib} store={createCollectionStore(memStore())} />);
    fireEvent.change(screen.getByLabelText(/search the library/i), { target: { value: "austen" } });
    expect(screen.getByText("1 book")).toBeInTheDocument();
    expect(screen.getByText("Pride and Prejudice")).toBeInTheDocument();
  });

  it("calls onOpenBook when a card is opened", () => {
    const onOpen = vi.fn();
    render(<LibraryWorkbench books={lib} onOpenBook={onOpen} store={createCollectionStore(memStore())} />);
    // BookCard is a role="button" whose accessible name starts with the title.
    fireEvent.click(screen.getByRole("button", { name: /^Moby Dick by Melville/ }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "a" }));
  });
});
