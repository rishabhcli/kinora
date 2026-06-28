#!/usr/bin/env bash
# Run the full verification matrix for the Kinora developer SDKs + docs.
# Mocks all HTTP — zero live calls. KINORA_LIVE_VIDEO is never touched.
#
#   bash clients/verify.sh
#
# Assumes (and checks for) the per-package dev installs documented in
# clients/README.md. Exits non-zero on the first failure.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "Spec — regenerate & check artifacts are in sync"
node clients/spec/generate.mjs --check
node clients/spec/sync-ts.mjs --check
node clients/spec/sync-py.mjs --check

step "TypeScript SDK — typecheck + tests"
( cd clients/typescript && npx tsc --noEmit && npx vitest run )

step "TypeScript — example typecheck"
( cd clients/typescript && npx tsc -p tsconfig.examples.json )

step "Python SDK — ruff + mypy + pytest"
PY=clients/python/.venv/bin
"$PY/ruff" check clients/python/src
"$PY/mypy" clients/python/src/kinora
"$PY/pytest" clients/python -q

step "Contract drift — static derivation vs the spec"
clients/python/.venv/bin/python clients/contract-drift/check_drift.py
clients/python/.venv/bin/pytest clients/contract-drift -q

step "Docs portal — build check"
node docs/portal/build/build.mjs --check

step "Docs — markdown renderer tests"
node --test "docs/portal/build/*.test.mjs"

printf '\n\033[32mAll checks passed.\033[0m\n'
