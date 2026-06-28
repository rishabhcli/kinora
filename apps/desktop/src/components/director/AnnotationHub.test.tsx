import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import AnnotationHub from "./AnnotationHub";
import { createAnnotationStore, type KeyValueStore } from "../../lib/api/annotations";

const memStore = (): KeyValueStore => {
  const m = new Map<string, string>();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
};

describe("AnnotationHub", () => {
  it("lists every thread for the book with the first comment", () => {
    const store = createAnnotationStore(memStore());
    store.open("b1", { shot_id: "s1" }, "Ada", "first note", ["look"]);
    store.open("b1", { word_range: [0, 5] }, "Grace", "second note");
    render(<AnnotationHub bookId="b1" annotations={store} />);
    expect(screen.getByText(/first note/)).toBeInTheDocument();
    expect(screen.getByText(/second note/)).toBeInTheDocument();
    // "#look" appears both as a filter chip (button) and in the thread's tag row.
    expect(screen.getByRole("button", { name: "#look" })).toBeInTheDocument();
  });

  it("filters to open vs resolved", () => {
    const store = createAnnotationStore(memStore());
    const t = store.open("b1", { shot_id: "s1" }, "Ada", "open one");
    store.open("b1", { shot_id: "s2" }, "Ada", "to resolve");
    const all = store.forBook("b1");
    const second = all.find((x) => x.id !== t.id)!;
    store.setResolved(second.id, true, "Ada");

    render(<AnnotationHub bookId="b1" annotations={store} />);
    fireEvent.click(screen.getByRole("button", { name: /open 1/i }));
    expect(screen.getByText(/open one/)).toBeInTheDocument();
    expect(screen.queryByText(/to resolve/)).not.toBeInTheDocument();
  });

  it("filters by tag", () => {
    const store = createAnnotationStore(memStore());
    store.open("b1", { shot_id: "s1" }, "Ada", "tagged note", ["continuity"]);
    store.open("b1", { shot_id: "s2" }, "Ada", "plain note");
    render(<AnnotationHub bookId="b1" annotations={store} />);
    fireEvent.click(screen.getByRole("button", { name: "#continuity" }));
    expect(screen.getByText(/tagged note/)).toBeInTheDocument();
    expect(screen.queryByText(/plain note/)).not.toBeInTheDocument();
  });

  it("jumps to a thread's shot", () => {
    const store = createAnnotationStore(memStore());
    store.open("b1", { shot_id: "shot-abc" }, "Ada", "note");
    const onJump = vi.fn();
    render(<AnnotationHub bookId="b1" annotations={store} onJumpToShot={onJump} />);
    fireEvent.click(screen.getByRole("button", { name: /jump to shot/i }));
    expect(onJump).toHaveBeenCalledWith("shot-abc");
  });

  it("shows an empty state", () => {
    render(<AnnotationHub bookId="b1" annotations={createAnnotationStore(memStore())} />);
    expect(screen.getByText(/no notes on this book yet/i)).toBeInTheDocument();
  });
});
