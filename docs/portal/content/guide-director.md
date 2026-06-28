# Director tools

Director mode lets a reader steer the film: comment on a shot, edit the canon, or
resolve a continuity conflict the crew surfaced. Each action triggers a
**surgical** regeneration — only the affected shots re-render; everything else
stays a cache hit.

## Region comments

A region comment is a natural-language note about a shot ("slower here", "make
the room warmer"). The backend classifies it (Cinematographer pacing/look vs
Continuity room/canon), re-rolls the shot's seed, enqueues a targeted regen, and
emits an `agent_activity` event. It also **learns your directing taste** from the
note.

```python
resp = client.director.comment(
    session_id,
    shot_id="shot_abc",
    note="hold this shot a beat longer, it feels rushed",
)
print(resp.agent, resp.aspect, resp.message)  # e.g. cinematographer pacing ...
for prior in resp.learned:                      # taste the note taught
    print(prior.label, prior.applied)           # "Slower shots" True
```

```ts
const resp = await client.director.comment(sessionId, {
  shot_id: "shot_abc",
  note: "hold this shot a beat longer",
});
```

Watch for the resulting `regen_done` event on the session stream.

## Editing the canon

Editing a canon entity (a character's appearance, a location's palette) writes a
new versioned entity and **surgically regenerates only the shots whose reference
set includes that entity**. The response tells you exactly which shots
re-rendered and how many were skipped (cache hits).

```python
canon = client.books.canon(book_id)
hero = next(e for e in canon.entities if e.name == "Eleanor")

edit = client.director.canon_edit(
    book_id,
    entity_key=hero.id,
    changes={"description": "now wears a deep crimson cloak"},
)
print("entity version:", edit.version)
print("re-rendering:", edit.affected_shot_ids)
print("cache hits skipped:", edit.skipped_shots)
```

```ts
const canon = await client.books.canon(bookId);
const hero = canon.entities.find((e) => e.name === "Eleanor")!;

const edit = await client.director.canonEdit(bookId, {
  entity_key: hero.id,
  changes: { description: "now wears a deep crimson cloak" },
});
```

Each dependent shot emits a `regen_done` event as it completes.

> **Lossless reference swaps.** A locked reference image is projected with both an
> ephemeral `oss_url` (for display) and a durable `oss_key`. Echo the `oss_key`
> back inside `changes.appearance` so a reference swap round-trips — the presigned
> URL cannot be re-stored, the key can.

## Resolving continuity conflicts

When the Continuity Supervisor catches a contradiction (e.g. *the heroine draws a
sword she lost three beats ago*), it raises a `conflict_choice` event with the
claim, the canon fact it violates, and the fixed policy options:

- `honor_canon` — regenerate the shot honouring the established canon,
- `evolve_canon` — assert the new state (if the text supports it) and regenerate,
- `surface_to_user` — leave it surfaced for the director to decide.

```python
choice = client.director.conflict_choice(
    session_id, conflict_id="cf_shot_abc", option="honor_canon",
)
print(choice.status, choice.reasoning)  # applied / deferred / already_resolved
```

```ts
const choice = await client.director.conflictChoice(sessionId, {
  conflict_id: "cf_shot_abc",
  option: "honor_canon",
});
```

Re-submitting the same choice is idempotent (`status: "already_resolved"`). The
session stream shows the Showrunner's arbitration as staged `agent_activity`
lines, then a `regen_done` with the fresh clip.

### Reloading the conflict log

A refreshed client can reload the session's surfaced conflicts + resolutions:

```python
for record in client.director.conflicts(session_id):
    print(record.conflict_id, record.resolved, record.chosen_option)
```

## Your directing style

The comments and edits feed a learned **directing style** — pacing, palette, and
framing priors that default future shots. Read and reset it per book or globally:

```python
style = client.prefs.me()                 # across all your books
book_style = client.prefs.book(book_id)   # for one book
for prior in style.priors:
    print(prior.label, prior.bias, "applied" if prior.applied else "")

client.prefs.reset_book(book_id)          # reset one book
client.prefs.reset_me()                    # reset everywhere
```
