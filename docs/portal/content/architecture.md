# The six-agent architecture

Kinora's consistency comes from architecture, not a bigger model. Six
single-purpose agents — each a separate service with a typed JSON contract — read
and write **one shared canon** through an MCP server. No agent holds private
mutable state; the canon is the only truth.

## The crew

| Agent | Job | Surfaces in the API as |
|---|---|---|
| **Showrunner** | Plans the production, decomposes the book, **arbitrates conflicts** | the `showrunner` author of `agent_activity` + conflict arbitration reasoning |
| **Adapter** | PDF → screenplay → shot list (with source spans) | the shots from `GET /books/{id}/shots`, each carrying a `source_span` |
| **Continuity Supervisor** | Owns canon writes; flags inconsistencies; runs forgetting/versioning | `GET /books/{id}/canon` entities + states; raises `conflict_choice` |
| **Cinematographer** | Designs each shot: keyframe, camera, locked references, render mode | a shot's `render_mode` + `reference_image_ids`; routes look/pacing comments |
| **Generator** | Renders the clip + narration | `clip_ready` / `scene_stitched` events; clip `oss_url`s |
| **Critic / QA** | Scores each clip against the canon; decides pass / fix / regen | a shot's `qa` block; drives `regen_done` |

## Two planes

Kinora separates a **control plane** (the Scheduler — decides *when and what* to
render against the reader's attention) from the **creative/data plane** (the crew
+ memory + infra — decides *how* a scene looks and produces the pixels). The
memory store sits at the centre as a shared blackboard.

As an API consumer you touch:

- the **control plane** through sessions + intent + seek (the watermark buffer,
  promotion, cancellation) — see [Generation-on-scroll](guide-generation-on-scroll.html);
- the **creative plane**'s outputs through events, films, the canon, and the
  director tools.

## The memory layer (the canon)

A versioned **canon graph** — characters, voices, locations, props, style,
timeline — plus an episodic store of every shot ever generated and its QA scores.
It is exposed (internally) through a small MCP tool surface, and (externally)
through `GET /books/{id}/canon`:

- **Recall under a limited context window** — a beat retrieves *only* what it
  needs (characters present + active location + style tokens + the previous
  shot's endpoint frame), never the whole book. Token cost stays flat as books
  grow.
- **Timely forgetting** — facts are scoped to the beat interval where they were
  true. A `CanonStateResponse` with `valid_to_beat != null` is a *retired* fact:
  it drops out of forward retrieval but survives for backward (time-travel) reads.
  The canon endpoint returns both active and retired states so "forgetting" is
  inspectable.
- **Preference learning** — every director edit writes a preference signal, so
  the system learns the reader's taste and applies it by default next time. See
  [Director tools](guide-director.html) and `GET /me/prefs`.
- **Free re-reads** — each shot has a content hash; a cache hit serves the clip
  for zero video-seconds, which makes director edits surgical: only the dependent
  shots regenerate (`affected_shot_ids` vs `skipped_shots` in a canon edit).

## The negotiation protocol

When the Continuity Supervisor catches a contradiction it raises a **structured
conflict object** and the Showrunner arbitrates under a fixed policy: evolve the
canon if the text supports it, surface to the director if user-facing, otherwise
honor the established truth. You drive that resolution through
`POST /sessions/{id}/conflict_choice` and watch it unfold as staged
`agent_activity` events, closing with a `regen_done`. This is the part a human
can *watch happen* — see [Director tools](guide-director.html#resolving-continuity-conflicts).

## The generation pipeline (where events come from)

1. **Phase A — ingest** (at import): extract pages + word boxes, analyse pages,
   build the canon, plan the shot list + source-span index, identity-lock
   keyframes + voices. Emits `ingest_progress`.
2. **Phase B — render a shot** (just-in-time): the Cinematographer designs the
   shot, the Generator renders the clip + narration, the Critic scores it against
   the canon and decides pass / fix / regen. Emits `keyframe_ready`,
   `clip_ready`, then `scene_stitched` once a scene's accepted shots are stitched.

The `sync_map` carried on stitched-film events binds each narrated word to a
film-timeline timestamp and a page bounding box — that is what powers the
karaoke-style word highlighting in the reader.
