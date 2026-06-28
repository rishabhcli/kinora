import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ContinueReadingRow from "./ContinueReadingRow";
import type { DiscoveryBook, Interaction } from "../../lib/discovery/types";

const DAY = 86_400_000;

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
  };
}

describe("ContinueReadingRow", () => {
  it("renders nothing when there are no in-progress books", () => {
    const { container } = render(<ContinueReadingRow books={[book({ progress: 0 })]} />);
    expect(container.firstChild).toBeNull();
  });

  it("orders by resume-worthiness (recency)", () => {
    const books = [book({ id: "old", title: "Old", progress: 50 }), book({ id: "new", title: "New", progress: 50 })];
    const history: Interaction[] = [
      { bookId: "old", kind: "open", at: 0 },
      { bookId: "new", kind: "open", at: 10 * DAY },
    ];
    render(<ContinueReadingRow books={books} history={history} now={10 * DAY} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("New");
  });

  it("shows progress and fires onOpen", () => {
    const onOpen = vi.fn();
    render(<ContinueReadingRow books={[book({ id: "a", title: "A", progress: 42 })]} now={0} onOpen={onOpen} />);
    expect(screen.getByText("42%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Resume A/ }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "a" }));
  });

  it("renders a relative last-read hint", () => {
    const history: Interaction[] = [{ bookId: "a", kind: "open", at: 0 }];
    render(<ContinueReadingRow books={[book({ id: "a", progress: 30 })]} history={history} now={2 * DAY} />);
    expect(screen.getByText("2d ago")).toBeInTheDocument();
  });
});
