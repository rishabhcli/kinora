import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import CanonVault from "./CanonVault";
import type { CanonGraph, DirectorShot } from "../../lib/api/director";

const canonEdit = vi.fn();
vi.mock("../../lib/api/director", async (orig) => {
  const actual = await orig<typeof import("../../lib/api/director")>();
  return {
    ...actual, // keep canonEditBlastRadius (pure helper used by the component)
    director: { canonEdit: (...a: unknown[]) => canonEdit(...a) },
  };
});

function graph(): CanonGraph {
  return {
    book_id: "b1",
    entities: [
      {
        id: "hero",
        type: "character",
        name: "Ishmael",
        aliases: [],
        description: "A sailor.",
        appearance: { description: "dark coat", reference_images: [] },
        style_tokens: null,
        voice: null,
        version: 1,
        valid_from_beat: 0,
        valid_to_beat: null,
        first_appearance: null,
      },
    ],
    states: [
      { id: "st1", subject_entity_key: "hero", predicate: "has", object_value: "a sword", valid_from_beat: 1, valid_to_beat: 34, version: 1, active: false, source_span: null },
    ],
    markdown: null,
  };
}

const shots: DirectorShot[] = [
  { shot_id: "s1", beat_id: "b1", scene_id: "sc1", source_span: null, status: "accepted", render_mode: "r2v", duration_s: 5, qa: null, clip_url: "u", reference_image_ids: ["hero@1"] },
  { shot_id: "s2", beat_id: "b2", scene_id: "sc1", source_span: null, status: "accepted", render_mode: "r2v", duration_s: 5, qa: null, clip_url: "u", reference_image_ids: ["ship"] },
];

beforeEach(() => canonEdit.mockReset());

describe("CanonVault", () => {
  it("shows entities, the blast radius, and retired facts", () => {
    render(<CanonVault bookId="b1" canon={graph()} shots={shots} />);
    expect(screen.getByText("Ishmael")).toBeInTheDocument();
    // 1 of the 2 shots references "hero"
    expect(screen.getByText(/1 dependent shot/)).toBeInTheDocument();
    expect(screen.getByText(/retired @ 34/)).toBeInTheDocument();
  });

  it("edits an entity → POSTs canon_edit → reports the surgical regen", async () => {
    canonEdit.mockResolvedValue({ entity_key: "hero", version: 2, affected_shot_ids: ["s1"], skipped_shots: 1 });
    const onEdited = vi.fn();
    render(<CanonVault bookId="b1" canon={graph()} shots={shots} onEdited={onEdited} />);

    fireEvent.click(screen.getByRole("button", { name: /edit canon/i }));
    fireEvent.change(screen.getByPlaceholderText("Description"), { target: { value: "A weathered sailor." } });
    fireEvent.click(screen.getByRole("button", { name: /save & re-render/i }));

    await waitFor(() =>
      expect(canonEdit).toHaveBeenCalledWith("b1", {
        entity_key: "hero",
        changes: { description: "A weathered sailor." },
      }),
    );
    expect(await screen.findByText(/1 shot re-rendering, 1 cache hits/i)).toBeInTheDocument();
    expect(onEdited).toHaveBeenCalledWith(["s1"]);
  });

  it("does not POST when nothing changed", async () => {
    render(<CanonVault bookId="b1" canon={graph()} shots={shots} />);
    fireEvent.click(screen.getByRole("button", { name: /edit canon/i }));
    fireEvent.click(screen.getByRole("button", { name: /save & re-render/i }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /save & re-render/i })).not.toBeInTheDocument());
    expect(canonEdit).not.toHaveBeenCalled();
  });
});
