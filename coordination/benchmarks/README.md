# Agent 07 — reproducible perf benchmarks

Real-number sources for `coordination/PERF.md`. No DashScope/network; `KINORA_LIVE_VIDEO` off.

| Script | Measures | Run |
|---|---|---|
| `ingest_token_bench.py` | input-token reduction on real prompts + contract schemas via `app.optim.prompt_compress` | `cd backend && DASHSCOPE_API_KEY=test .venv/bin/python ../coordination/benchmarks/ingest_token_bench.py` |
| `render_throughput_bench.py` | ffmpeg Ken-Burns degrade lane: serial vs `optim.batch.gather_bounded` | `cd backend && DASHSCOPE_API_KEY=test .venv/bin/python ../coordination/benchmarks/render_throughput_bench.py` |
| `fps_harness.html` | reading-room animation primitives: rAF fps + per-frame main-thread work (composited vs layout-thrash) | drive headless via playwright-core (`fps_driver.js` pattern), 720×1280 viewport |

`fps_driver.js` (not committed; ~20 lines): `chromium.launch({executablePath:<chrome-for-testing>})`,
`newPage({viewport:{width:720,height:1280}})`, `goto(file://fps_harness.html)`,
`evaluate(() => window.runFPS('composited',180))` and `'thrash'`. Bundle/migration numbers come from
`pnpm exec vite build` and `alembic upgrade head` + `EXPLAIN ANALYZE` (see PERF.md).
