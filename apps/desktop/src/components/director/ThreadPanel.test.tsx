import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ThreadPanel from "./ThreadPanel";
import { createAnnotationStore, type KeyValueStore } from "../../lib/api/annotations";

const memStore = (): KeyValueStore => {
  const m = new Map<string, string>();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
};

describe("ThreadPanel", () => {
  it("opens a thread from the composer and renders it", () => {
    const store = createAnnotationStore(memStore());
    render(<ThreadPanel bookId="b1" anchor={{ shot_id: "s1" }} annotations={store} author="Ada" />);

    expect(screen.getByText(/no notes yet/i)).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText(/add a note/i), { target: { value: "Make it warmer" } });
    fireEvent.click(screen.getByRole("button", { name: "Post" }));

    expect(screen.getByText("Make it warmer")).toBeInTheDocument();
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(store.forBook("b1")).toHaveLength(1);
  });

  it("resolves and reopens a thread", () => {
    const store = createAnnotationStore(memStore());
    store.open("b1", { shot_id: "s1" }, "Ada", "note");
    render(<ThreadPanel bookId="b1" anchor={{ shot_id: "s1" }} annotations={store} author="Ada" />);

    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(store.forBook("b1")[0].resolved).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    expect(store.forBook("b1")[0].resolved).toBe(false);
  });

  it("only shows threads anchored to the given shot", () => {
    const store = createAnnotationStore(memStore());
    store.open("b1", { shot_id: "s1" }, "Ada", "for s1");
    store.open("b1", { shot_id: "s2" }, "Ada", "for s2");
    render(<ThreadPanel bookId="b1" anchor={{ shot_id: "s1" }} annotations={store} author="Ada" />);
    expect(screen.getByText("for s1")).toBeInTheDocument();
    expect(screen.queryByText("for s2")).not.toBeInTheDocument();
  });
});
