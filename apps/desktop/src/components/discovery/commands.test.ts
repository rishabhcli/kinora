import { describe, it, expect, vi } from "vitest";
import { buildCommands } from "./commands";
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
  };
}

const books = [
  book({ id: "dune", title: "Dune", author: "Herbert", genre: "SF" }),
  book({ id: "pride", title: "Pride and Prejudice", author: "Austen", genre: "Romance" }),
];

const navTargets = [{ label: "Home" }, { label: "Library" }];

describe("buildCommands", () => {
  it("creates nav, action, and per-book commands", () => {
    const cmds = buildCommands({
      navTargets,
      navigate: () => {},
      books,
      openBook: () => {},
      openSearch: () => {},
      resume: () => {},
    });
    expect(cmds.some((c) => c.id === "nav-Home")).toBe(true);
    expect(cmds.some((c) => c.id === "book-dune")).toBe(true);
    expect(cmds.some((c) => c.id === "action-resume")).toBe(true);
    expect(cmds.some((c) => c.id === "action-search")).toBe(true);
  });

  it("surfaces recent books first and dedupes them from the book list", () => {
    const cmds = buildCommands({
      navTargets,
      navigate: () => {},
      books,
      recents: ["pride"],
      openBook: () => {},
    });
    expect(cmds.some((c) => c.id === "recent-pride")).toBe(true);
    // not duplicated as a plain book command
    expect(cmds.some((c) => c.id === "book-pride")).toBe(false);
    // recents come before nav
    expect(cmds.findIndex((c) => c.group === "recent")).toBeLessThan(
      cmds.findIndex((c) => c.group === "navigation"),
    );
  });

  it("wires run callbacks", () => {
    const navigate = vi.fn();
    const openBook = vi.fn();
    const cmds = buildCommands({ navTargets, navigate, books, openBook });
    cmds.find((c) => c.id === "nav-Library")!.run();
    cmds.find((c) => c.id === "book-dune")!.run();
    expect(navigate).toHaveBeenCalledWith("Library");
    expect(openBook).toHaveBeenCalledWith(expect.objectContaining({ id: "dune" }));
  });

  it("omits optional actions when not provided", () => {
    const cmds = buildCommands({ navTargets, navigate: () => {}, books, openBook: () => {} });
    expect(cmds.some((c) => c.id === "action-resume")).toBe(false);
    expect(cmds.some((c) => c.id === "action-search")).toBe(false);
  });
});
