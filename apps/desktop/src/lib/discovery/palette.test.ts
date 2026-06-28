import { describe, it, expect, vi } from "vitest";
import {
  rankCommands,
  groupRanked,
  moveSelection,
  GROUP_LABELS,
  type Command,
} from "./palette";

function cmd(over: Partial<Command> = {}): Command {
  return {
    id: over.id ?? "id",
    title: over.title ?? "Title",
    group: over.group ?? "action",
    keywords: over.keywords,
    hint: over.hint,
    run: over.run ?? (() => {}),
  };
}

const commands: Command[] = [
  cmd({ id: "home", title: "Go to Home", group: "navigation", keywords: ["dashboard"] }),
  cmd({ id: "library", title: "Go to Library", group: "navigation" }),
  cmd({ id: "settings", title: "Open Settings", group: "setting", keywords: ["preferences"] }),
  cmd({ id: "dune", title: "Dune", group: "book", keywords: ["Frank Herbert"] }),
  cmd({ id: "upload", title: "Upload a Book", group: "action" }),
];

describe("rankCommands", () => {
  it("returns default order (recents/nav first) for an empty query", () => {
    const r = rankCommands(commands, "");
    // navigation group is boosted above action/setting/book
    expect(r[0].command.group).toBe("navigation");
    expect(r.length).toBe(commands.length);
  });

  it("fuzzy-matches the title", () => {
    const r = rankCommands(commands, "dune");
    expect(r[0].command.id).toBe("dune");
  });

  it("matches via keywords", () => {
    const r = rankCommands(commands, "herbert");
    expect(r.map((x) => x.command.id)).toContain("dune");
  });

  it("drops non-matching commands", () => {
    const r = rankCommands(commands, "zzzz");
    expect(r).toEqual([]);
  });

  it("ranks a navigation match above a book with a similar score", () => {
    const r = rankCommands(commands, "go to library");
    expect(r[0].command.id).toBe("library");
  });

  it("is stable within equal scores (registration order)", () => {
    const two = [cmd({ id: "a", title: "Watch" }), cmd({ id: "b", title: "Watch" })];
    const r = rankCommands(two, "watch");
    expect(r.map((x) => x.command.id)).toEqual(["a", "b"]);
  });

  it("runs the command's side effect when invoked", () => {
    const run = vi.fn();
    const r = rankCommands([cmd({ id: "x", title: "Do It", run })], "do it");
    r[0].command.run();
    expect(run).toHaveBeenCalledOnce();
  });
});

describe("groupRanked", () => {
  it("buckets ranked commands by group, preserving first-seen order", () => {
    const r = rankCommands(commands, "");
    const grouped = groupRanked(r);
    expect(grouped[0].group).toBe("navigation");
    expect(grouped.every((g) => g.items.length > 0)).toBe(true);
    expect(GROUP_LABELS.navigation).toBe("Go to");
  });
});

describe("moveSelection", () => {
  it("wraps in both directions", () => {
    expect(moveSelection(0, -1, 3)).toBe(2);
    expect(moveSelection(2, 1, 3)).toBe(0);
    expect(moveSelection(1, 1, 3)).toBe(2);
  });
  it("handles an empty list", () => {
    expect(moveSelection(0, 1, 0)).toBe(0);
  });
});
