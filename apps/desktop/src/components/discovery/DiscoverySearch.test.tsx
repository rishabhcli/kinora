import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import DiscoverySearch from "./DiscoverySearch";
import type { DiscoveryBook } from "../../lib/discovery/types";

function book(over: Partial<DiscoveryBook> = {}): DiscoveryBook {
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
    genre: over.genre,
    era: over.era,
  };
}

const lib: DiscoveryBook[] = [
  book({ id: "dune", title: "Dune", author: "Frank Herbert", genre: "Science Fiction", era: "20th century" }),
  book({ id: "neuro", title: "Neuromancer", author: "William Gibson", genre: "Science Fiction", era: "20th century" }),
  book({ id: "pride", title: "Pride and Prejudice", author: "Jane Austen", genre: "Romance", era: "19th century", progress: 100 }),
  book({ id: "moby", title: "Moby Dick", author: "Herman Melville", genre: "Adventure", era: "19th century", progress: 40 }),
];

function facetButton(label: string): HTMLElement {
  const match = screen
    .getAllByRole("checkbox")
    .find((b) => (b.textContent ?? "").includes(label));
  if (!match) throw new Error(`facet not found: ${label}`);
  return match;
}

describe("DiscoverySearch", () => {
  it("shows all books and a count by default", () => {
    render(<DiscoverySearch books={lib} />);
    expect(screen.getByTestId("result-count")).toHaveTextContent("4 results");
    expect(screen.getByText("Dune")).toBeInTheDocument();
  });

  it("filters by free-text query", () => {
    render(<DiscoverySearch books={lib} />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "dune" } });
    expect(screen.getByText("Dune")).toBeInTheDocument();
    expect(screen.queryByText("Moby Dick")).not.toBeInTheDocument();
  });

  it("filters by a genre facet (counts shown)", () => {
    render(<DiscoverySearch books={lib} />);
    fireEvent.click(facetButton("Science Fiction"));
    expect(screen.getByTestId("result-count")).toHaveTextContent("2 results");
    expect(screen.getByText("Dune")).toBeInTheDocument();
    expect(screen.queryByText("Pride and Prejudice")).not.toBeInTheDocument();
  });

  it("filters by reading status", () => {
    render(<DiscoverySearch books={lib} />);
    fireEvent.click(facetButton("Finished"));
    expect(screen.getByTestId("result-count")).toHaveTextContent("1 result");
    expect(screen.getByText("Pride and Prejudice")).toBeInTheDocument();
  });

  it("AND-combines facets and offers a Clear control", () => {
    render(<DiscoverySearch books={lib} />);
    fireEvent.click(facetButton("Science Fiction"));
    fireEvent.click(facetButton("20th century"));
    expect(screen.getByTestId("result-count")).toHaveTextContent("2 results");
    const clear = screen.getByRole("button", { name: /Clear \(2\)/ });
    fireEvent.click(clear);
    expect(screen.getByTestId("result-count")).toHaveTextContent("4 results");
  });

  it("surfaces SF via semantic synonym ('space') in semantic mode", () => {
    render(<DiscoverySearch books={lib} mode="semantic" />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "space" } });
    // dune + neuromancer match via the space→science/fiction synonym
    expect(screen.getByText("Dune")).toBeInTheDocument();
    expect(screen.getByText("Neuromancer")).toBeInTheDocument();
    expect(screen.queryByText("Pride and Prejudice")).not.toBeInTheDocument();
  });

  it("shows an empty state when nothing matches", () => {
    render(<DiscoverySearch books={lib} />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "zebra" } });
    expect(screen.getByText(/Nothing matched/)).toBeInTheDocument();
  });

  it("offers a 'did you mean' suggestion when a query yields nothing, applied on click", () => {
    render(<DiscoverySearch books={lib} mode="exact" />);
    // "dune zztop" → AND semantics: 'zztop' matches nothing → zero results; the
    // first token resolves a close title for the suggestion.
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "dune zztop" } });
    expect(screen.getByText(/Nothing matched/)).toBeInTheDocument();
    const suggest = screen.getByRole("button", { name: "Dune" });
    fireEvent.click(suggest);
    expect(screen.getByText("Dune")).toBeInTheDocument();
    expect(screen.getByTestId("result-count")).toHaveTextContent(/result/);
  });

  it("seeds the query from initialQuery", () => {
    render(<DiscoverySearch books={lib} initialQuery="neuromancer" />);
    expect(screen.getByText("Neuromancer")).toBeInTheDocument();
    expect(screen.queryByText("Dune")).not.toBeInTheDocument();
  });

  it("opens a book through the card", () => {
    const onOpen = vi.fn();
    render(<DiscoverySearch books={lib} onOpen={onOpen} initialQuery="dune" />);
    const card = screen.getByTestId("preview-card-dune");
    fireEvent.click(within(card).getByRole("button", { name: /Dune/ }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "dune" }));
  });
});
