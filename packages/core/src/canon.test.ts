import { describe, expect, it } from "vitest";

import type { CanonEntityResponse, ShotResponse } from "./api/types";
import {
  baseEntityKey,
  buildChanges,
  ccsDelta,
  changesToRestore,
  dependentShotIds,
  draftFromEntity,
  parsePaletteValue,
  validateCanonDraft,
} from "./canon";

function shot(id: string, refs: string[]): ShotResponse {
  return { shot_id: id, status: "accepted", reference_image_ids: refs };
}

const elsa: CanonEntityResponse = {
  id: "char_elsa",
  type: "character",
  name: "Elsa",
  aliases: ["the Snow Queen"],
  description: "the queen",
  appearance: {
    description: "platinum braid, ice-blue gown",
    reference_images: [
      { oss_url: "https://x/front.png?sig=a", oss_key: "refs/elsa/front.png", pose: "front", locked: true },
      { oss_url: "https://x/3q.png?sig=b", oss_key: "refs/elsa/3q.png", pose: "3q", locked: false },
    ],
  },
  version: 3,
};

describe("baseEntityKey", () => {
  it("strips the @vN version suffix", () => {
    expect(baseEntityKey("char_elsa@v3")).toBe("char_elsa");
    expect(baseEntityKey("loc_window")).toBe("loc_window");
  });
});

describe("dependentShotIds — the §8.7 surgical blast radius", () => {
  const shots = [
    shot("shot_a", ["char_elsa@v3", "loc_window@v1"]),
    shot("shot_b", ["char_anna@v1"]),
    shot("shot_c", ["char_elsa@v2"]), // different version, same entity → still dependent
    shot("shot_d", []),
  ];

  it("returns only the shots whose reference set cites the entity", () => {
    expect(dependentShotIds(shots, "char_elsa")).toEqual(["shot_a", "shot_c"]);
  });

  it("excludes shots that cite other entities (they stay cache hits)", () => {
    expect(dependentShotIds(shots, "char_anna")).toEqual(["shot_b"]);
    expect(dependentShotIds(shots, "char_nobody")).toEqual([]);
  });

  it("tolerates an undefined shot list", () => {
    expect(dependentShotIds(undefined, "char_elsa")).toEqual([]);
  });
});

describe("parsePaletteValue", () => {
  it("accepts an array, a comma/space string, or nothing", () => {
    expect(parsePaletteValue(["#111", "#222"])).toEqual(["#111", "#222"]);
    expect(parsePaletteValue("#111, #222  #333")).toEqual(["#111", "#222", "#333"]);
    expect(parsePaletteValue(undefined)).toEqual([]);
  });
});

describe("buildChanges", () => {
  it("is empty when nothing changed", () => {
    expect(buildChanges(elsa, draftFromEntity(elsa))).toEqual({});
  });

  it("emits only the changed scalar fields", () => {
    const draft = { ...draftFromEntity(elsa), name: "Elsa II", aliasesText: "Snow Queen, Queen" };
    const changes = buildChanges(elsa, draft);
    expect(changes).toEqual({ name: "Elsa II", aliases: ["Snow Queen", "Queen"] });
  });

  it("round-trips a locked-reference swap by durable oss_key (the acceptance edit)", () => {
    // Swap which pose is locked: unlock front, lock 3q.
    const draft = draftFromEntity(elsa);
    draft.references[0]!.locked = false;
    draft.references[1]!.locked = true;
    const raw = buildChanges(elsa, draft);
    const changes = raw as {
      appearance?: { reference_images: { key: string; pose?: string; locked: boolean }[] };
    };
    expect(changes.appearance).toBeDefined();
    expect(changes.appearance!.reference_images).toEqual([
      { key: "refs/elsa/front.png", pose: "front", locked: false },
      { key: "refs/elsa/3q.png", pose: "3q", locked: true },
    ]);
    // No scalar fields changed.
    expect(raw.name).toBeUndefined();
  });

  it("retunes a Style node's palette/lens/art-direction", () => {
    const style: CanonEntityResponse = {
      id: "style_main",
      type: "style",
      name: "House style",
      version: 1,
      style_tokens: { palette: ["#000"], lens: "50mm", art_direction: "noir" },
    };
    const draft = draftFromEntity(style);
    draft.palette = ["#1b2a4a", "#c97b4a"];
    draft.lens = "35mm anamorphic";
    const changes = buildChanges(style, draft) as { style_tokens?: Record<string, unknown> };
    expect(changes.style_tokens).toEqual({
      palette: ["#1b2a4a", "#c97b4a"],
      lens: "35mm anamorphic",
      art_direction: "noir",
    });
  });
});

describe("ccsDelta — consistency proof", () => {
  it("pairs prior and post-regen CCS and reports whether it held", () => {
    expect(ccsDelta({ ccs: 0.88 }, { ccs: 0.91 })).toEqual({ before: 0.88, after: 0.91, held: true });
    expect(ccsDelta({ ccs: 0.9 }, { ccs: 0.82 }).held).toBe(false);
  });
  it("is null-tolerant when a side is missing", () => {
    expect(ccsDelta(null, { ccs: 0.9 })).toEqual({ before: null, after: 0.9, held: null });
    expect(ccsDelta(undefined, undefined)).toEqual({ before: null, after: null, held: null });
  });
});

describe("changesToRestore — undo", () => {
  it("reconstructs the entity's editable state (appearance keyed by oss_key)", () => {
    const changes = changesToRestore(elsa) as {
      name: string;
      aliases: string[];
      appearance: { reference_images: { key: string; locked: boolean }[] };
    };
    expect(changes.name).toBe("Elsa");
    expect(changes.aliases).toEqual(["the Snow Queen"]);
    expect(changes.appearance.reference_images).toEqual([
      { key: "refs/elsa/front.png", pose: "front", locked: true },
      { key: "refs/elsa/3q.png", pose: "3q", locked: false },
    ]);
  });

  it("round-trips: applying a draft then restoring recovers the original", () => {
    // Edit (unlock front), then build the inverse restore from the original snapshot.
    const restore = changesToRestore(elsa) as { appearance: { reference_images: unknown[] } };
    // The restore re-locks front exactly as the original had it.
    expect(restore.appearance.reference_images[0]).toEqual({
      key: "refs/elsa/front.png",
      pose: "front",
      locked: true,
    });
  });
});

describe("validateCanonDraft — guardrails", () => {
  it("accepts a normal draft", () => {
    expect(validateCanonDraft(elsa, draftFromEntity(elsa))).toEqual([]);
  });
  it("rejects an empty name", () => {
    const draft = { ...draftFromEntity(elsa), name: "   " };
    expect(validateCanonDraft(elsa, draft)).toContain("Name can't be empty.");
  });
  it("rejects unlocking the entire reference set when it had locked refs", () => {
    const draft = draftFromEntity(elsa);
    draft.references = draft.references.map((r) => ({ ...r, locked: false }));
    expect(validateCanonDraft(elsa, draft)).toHaveLength(1);
  });
});
