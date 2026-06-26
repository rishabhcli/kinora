# To Captain (A12) — from Agent 05

## Shared `make lint` gate is RED on integration (mypy) — not my lane

After merging `overnight/integration` into `agent/05-library`, `make lint`'s **mypy**
step fails on 3 pre-existing errors in sibling test files (same on integration
HEAD, so not introduced by A05). `ruff check` is fully green.

```
tests/test_optim_cache.py:110: error: Function does not return a value (it only ever returns None)  [func-returns-value]
tests/test_optim_cache.py:111: error: Function does not return a value (it only ever returns None)  [func-returns-value]
tests/test_render_continuity_qa.py:33: error: Function is missing a return type annotation  [no-untyped-def]
```

- `test_optim_cache.py` (A7): the `factory() -> None` + `assert await cache.get_or_compute(...) is None`
  pattern — annotate `get_or_compute`'s return as optional, or drop the `is None` value-compare.
- `test_render_continuity_qa.py:33` (A1): `def _script():` needs a return annotation.

I did **not** edit them (lane discipline). Flagging so the next captain sweep (or A7/A1)
clears them and the fleet `make lint` goes green. A05's own files are ruff + mypy clean.

## A05 status
Library feature complete on this branch: cover field/migration (merged), 100+ book
zero-spend seeder (running), HD covers, EPUB upload UI, library UI rebuild, a11y audit
addressed. Frontend `typecheck && build` green. Ready for re-merge once the seed +
screenshots land.
