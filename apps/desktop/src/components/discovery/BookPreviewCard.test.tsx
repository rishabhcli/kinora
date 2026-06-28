import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import BookPreviewCard from "./BookPreviewCard";
import type { DiscoveryBook } from "../../lib/discovery/types";

function book(over: Partial<DiscoveryBook> = {}): DiscoveryBook {
  return {
    id: over.id ?? "dune",
    title: over.title ?? "Dune",
    author: over.author ?? "Frank Herbert",
    progress: over.progress ?? 0,
    coverColor: "#000",
    coverGradient: "g",
    coverImage: "",
    textColor: "#fff",
    spineColor: "#000",
    genre: over.genre ?? "Science Fiction",
    era: over.era,
  };
}

const config = { openDelayMs: 100, closeDelayMs: 50 };

afterEach(() => {
  vi.useRealTimers();
});

describe("BookPreviewCard", () => {
  it("renders the cover card with an accessible name", () => {
    render(<BookPreviewCard book={book()} />);
    expect(screen.getByRole("button", { name: /Dune by Frank Herbert/ })).toBeInTheDocument();
  });

  it("opens the preview only after the hover-intent delay", () => {
    vi.useFakeTimers();
    render(<BookPreviewCard book={book()} config={config} />);
    const card = screen.getByTestId("preview-card-dune");
    fireEvent.mouseEnter(card);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(120); });
    expect(screen.getByRole("dialog", { name: /Dune preview/ })).toBeInTheDocument();
  });

  it("does not open if the pointer leaves before the delay", () => {
    vi.useFakeTimers();
    render(<BookPreviewCard book={book()} config={config} />);
    const card = screen.getByTestId("preview-card-dune");
    fireEvent.mouseEnter(card);
    act(() => { vi.advanceTimersByTime(40); });
    fireEvent.mouseLeave(card);
    act(() => { vi.advanceTimersByTime(200); });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("fires onPreview once when the preview opens", () => {
    vi.useFakeTimers();
    const onPreview = vi.fn();
    render(<BookPreviewCard book={book()} config={config} onPreview={onPreview} />);
    fireEvent.mouseEnter(screen.getByTestId("preview-card-dune"));
    act(() => { vi.advanceTimersByTime(120); });
    expect(onPreview).toHaveBeenCalledTimes(1);
  });

  it("opens the book on click and on Enter", () => {
    const onOpen = vi.fn();
    render(<BookPreviewCard book={book()} onOpen={onOpen} />);
    const btn = screen.getByRole("button", { name: /Dune by/ });
    fireEvent.click(btn);
    fireEvent.keyDown(btn, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("exposes More like this / Not interested actions in the preview", () => {
    vi.useFakeTimers();
    const onMore = vi.fn();
    const onNot = vi.fn();
    render(<BookPreviewCard book={book()} config={config} onMoreLikeThis={onMore} onNotInterested={onNot} />);
    fireEvent.mouseEnter(screen.getByTestId("preview-card-dune"));
    act(() => { vi.advanceTimersByTime(120); });
    fireEvent.click(screen.getByRole("button", { name: /More like this/ }));
    expect(onMore).toHaveBeenCalledWith(expect.objectContaining({ id: "dune" }));
    // reopen for the dismiss action
    fireEvent.mouseEnter(screen.getByTestId("preview-card-dune"));
    act(() => { vi.advanceTimersByTime(120); });
    fireEvent.click(screen.getByRole("button", { name: /Not interested/ }));
    expect(onNot).toHaveBeenCalledWith(expect.objectContaining({ id: "dune" }));
  });

  it("shows a Resume label for in-progress books", () => {
    vi.useFakeTimers();
    render(<BookPreviewCard book={book({ progress: 40 })} config={config} />);
    fireEvent.mouseEnter(screen.getByTestId("preview-card-dune"));
    act(() => { vi.advanceTimersByTime(120); });
    expect(screen.getByRole("button", { name: /Resume/ })).toBeInTheDocument();
  });
});
