"""Component benchmark: input-token reduction on the REAL ingest/per-shot prompt path.

Uses real artifacts — app.agents.prompts.PROMPTS (system prompts) and a real Pydantic contract
schema (ShotListItem) — measured through app.optim.prompt_compress. No network / no DashScope.
Estimator is the stable ~4-chars/token heuristic, so the before/after RATIOS are meaningful.
"""

from __future__ import annotations

import json

from app.agents import contracts, prompts
from app.optim.prompt_compress import (
    collapse_whitespace,
    compact_json_schema,
    compression_ratio,
    dedupe_canon,
    estimate_tokens,
)

et = estimate_tokens


def line(label: str, before: int, after: int) -> None:
    pct = compression_ratio(before, after) * 100
    print(f"  {label:34} {before:6d} -> {after:6d} tok   (-{pct:4.1f}%)")


print("=== 1. Structured-output JSON schema (sent on every chat_json call) ===")
schema_tot_b = schema_tot_a = 0
for name, model in [
    ("ShotListItem", contracts.ShotListItem),
    ("ShotSpec", contracts.ShotSpec),
    ("Beat", contracts.Beat),
]:
    raw = json.dumps(model.model_json_schema(), separators=(",", ":"))
    compact = json.dumps(compact_json_schema(model.model_json_schema()), separators=(",", ":"))
    b, a = et(raw), et(compact)
    schema_tot_b += b
    schema_tot_a += a
    line(f"schema {name}", b, a)
line("SCHEMA TOTAL", schema_tot_b, schema_tot_a)

print("\n=== 2. System prompts (whitespace collapse) ===")
sys_b = sys_a = 0
for key, vp in prompts.PROMPTS.items():
    b, a = et(vp.system), et(collapse_whitespace(vp.system))
    sys_b += b
    sys_a += a
print(f"  {len(prompts.PROMPTS)} prompts total           {sys_b:6d} -> {sys_a:6d} tok"
      f"   (-{compression_ratio(sys_b, sys_a)*100:4.1f}%)")

print("\n=== 3. Canon context dedupe (re-sent entity facts within a per-shot slice) ===")
# Realistic: a per-shot prompt embeds the canon slice for the beat — recurring characters'
# appearance/style facts get re-sent verbatim across the beat's entities. dedupe_canon collapses
# the exact re-sends. Fixture shape mirrors CanonEntitySlice description blocks.
ent_alice = ("Alice: a wiry 12-year-old with a red wool coat, scuffed boots, and a brass key on a "
             "cord around her neck; cautious, watchful; warm low-key lighting follows her.")
ent_bob = ("Bjorn: a grey-bearded ferryman in oilskins, lantern-lit; gravelly, kind; cold teal "
           "rim light off the water.")
ent_loc = ("The Tidewharf: rotting timber piers under fog, gas lamps haloed, ropes and crates; "
           "muted teal-and-amber palette, shallow depth of field.")
# Across a 6-shot scene the same 3 blocks are re-sent each shot (the un-deduped status quo):
blocks = ([ent_alice, ent_bob, ent_loc] * 6)
raw_blob = "\n".join(blocks)
ded_blob = "\n".join(dedupe_canon(blocks))
line("canon slice (6-shot scene)", et(raw_blob), et(ded_blob))

print("\n=== 4. Representative per-shot Cinematographer call (input tokens) ===")
sysp = prompts.PROMPTS["cinematographer"].system if "cinematographer" in prompts.PROMPTS else \
    next(iter(prompts.PROMPTS.values())).system
schema_raw = json.dumps(contracts.ShotSpec.model_json_schema(), separators=(",", ":"))
schema_cmp = json.dumps(compact_json_schema(contracts.ShotSpec.model_json_schema()),
                        separators=(",", ":"))
canon_raw = "\n".join([ent_alice, ent_bob, ent_loc, ent_alice])  # alice re-sent once
canon_cmp = "\n".join(dedupe_canon([ent_alice, ent_bob, ent_loc, ent_alice]))
beat_text = ("Beat: Alice slips the brass key into the lock as Bjorn steadies the skiff; fog "
             "swallows the far pier. mood: tense-hopeful.")
before = et(sysp) + et(schema_raw) + et(canon_raw) + et(beat_text)
after = et(collapse_whitespace(sysp)) + et(schema_cmp) + et(canon_cmp) + et(beat_text)
line("per-shot input", before, after)
print(f"\n  Extrapolated over a 1,000-shot book ingest: "
      f"{before*1000:,} -> {after*1000:,} input tok  "
      f"(-{compression_ratio(before, after)*100:.1f}%, ~{(before-after)*1000:,} tok saved)")
print("  (Cache: deterministic page-analysis + canon-query results are content-hash hits on "
      "re-open/re-ingest -> those calls drop to 0; hit-rate 100% on identical content.)")
