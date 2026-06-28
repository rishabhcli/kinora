import { describe, it, expect } from "vitest";
import {
  encodeShareLink,
  decodeShareLink,
  buildProjectExport,
  isProjectExport,
  exportFilename,
  canonToMarkdown,
  type ShareTarget,
} from "./sharing";
import type { CanonGraph } from "./director";

describe("share links", () => {
  it("round-trips a shot deep link with a timestamp", () => {
    const target: ShareTarget = { kind: "shot", book_id: "b1", scene_id: "sc1", shot_id: "s1", t: 12.5 };
    const link = encodeShareLink(target);
    expect(link.startsWith("kinora://shot?")).toBe(true);
    expect(decodeShareLink(link)).toEqual(target);
  });

  it("round-trips a bare book link", () => {
    const target: ShareTarget = { kind: "book", book_id: "abc" };
    expect(decodeShareLink(encodeShareLink(target))).toEqual(target);
  });

  it("rejects malformed links", () => {
    expect(decodeShareLink("https://example.com")).toBeNull();
    expect(decodeShareLink("kinora://bogus?book=x")).toBeNull();
    expect(decodeShareLink("kinora://shot")).toBeNull();
    expect(decodeShareLink("kinora://book?scene=only")).toBeNull(); // no book id
  });
});

describe("project export", () => {
  it("builds and validates a project bundle", () => {
    const bundle = buildProjectExport("b1", { collections: [] }, 1234);
    expect(bundle).toMatchObject({ v: 1, kind: "kinora.director.project", book_id: "b1", exported_at: 1234 });
    expect(isProjectExport(bundle)).toBe(true);
    expect(isProjectExport({ v: 1 })).toBe(false);
    expect(isProjectExport(null)).toBe(false);
  });

  it("sanitizes export filenames", () => {
    expect(exportFilename("canon", "Pride & Prejudice!!")).toBe("canon-pride-prejudice.json");
    expect(exportFilename("notes", "", "md")).toBe("notes-book.md");
  });
});

describe("canonToMarkdown", () => {
  const canon: CanonGraph = {
    book_id: "b1",
    entities: [
      {
        id: "hero",
        type: "character",
        name: "Ishmael",
        aliases: ["the narrator"],
        description: "A wandering sailor.",
        appearance: { description: "Weathered, dark coat", reference_images: [] },
        style_tokens: null,
        voice: null,
        version: 2,
        valid_from_beat: 0,
        valid_to_beat: null,
        first_appearance: null,
      },
    ],
    states: [
      { id: "st1", subject_entity_key: "hero", predicate: "carries", object_value: "a sea-bag", valid_from_beat: 1, valid_to_beat: null, version: 1, active: true, source_span: null },
      { id: "st2", subject_entity_key: "hero", predicate: "has", object_value: "a sword", valid_from_beat: 1, valid_to_beat: 34, version: 1, active: false, source_span: null },
    ],
    markdown: null,
  };

  it("synthesizes markdown when the backend vault is absent", () => {
    const md = canonToMarkdown(canon);
    expect(md).toContain("# Canon — b1");
    expect(md).toContain("Ishmael");
    expect(md).toContain("the narrator");
    expect(md).toContain("Continuity (active)");
    expect(md).toContain("Continuity (retired");
    expect(md).toContain("~~"); // retired facts struck through
  });

  it("prefers the backend markdown when present", () => {
    expect(canonToMarkdown({ ...canon, markdown: "# Real Vault" })).toBe("# Real Vault");
  });
});
