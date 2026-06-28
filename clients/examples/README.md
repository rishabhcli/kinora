# Runnable examples

End-to-end examples for both SDKs. **They default to a built-in mock** so they
run with no live backend and never spend video credits (`KINORA_LIVE_VIDEO`
stays off — the examples never even touch a render path).

Point an example at a real backend by setting `KINORA_BASE_URL` (and optionally
`KINORA_EMAIL` / `KINORA_PASSWORD`); leave it unset to use the mock.

## TypeScript

```bash
cd clients/typescript && npm install   # once, for the SDK + tsx
# from the repo root:
npx --prefix clients/typescript tsx clients/examples/reading_session.ts
```

## Python

```bash
cd clients/python && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python ../examples/reading_session.py
.venv/bin/python ../examples/async_reading_session.py
```

| Example | What it shows |
|---|---|
| `reading_session.ts` / `.py` | login → list books → open session → intent → stream events |
| `async_reading_session.py` | the same loop on the async Python client |
| `director_canon_edit.ts` | a surgical canon edit and watching the regens |

Each example prints what it does, so you can read the transcript without a
backend.
