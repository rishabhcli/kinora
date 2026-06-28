"""Pytest wrapper for the contract-drift checker.

Runs the same static derivation the CLI does, and asserts the SDKs + spec match
the live FastAPI route surface. Also asserts the generated artifacts (the TS/Py
spec modules + openapi.json) are in sync with the source-of-truth catalog, and
exercises the diff logic directly so a regression in the checker itself is
caught. No backend install needed (static AST parse); the route-drift test skips
cleanly if the backend route dir is absent.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

from check_drift import (  # noqa: E402
    ROUTES_DIR,
    compute_drift,
    derive_live_endpoints_static,
    load_catalog_endpoints,
)

_NODE = shutil.which("node")


@pytest.mark.skipif(not ROUTES_DIR.is_dir(), reason="backend route dir not present")
def test_no_drift_between_spec_and_live_routes() -> None:
    documented = load_catalog_endpoints()
    live = derive_live_endpoints_static()
    report = compute_drift(live, documented)
    assert report.in_sync, (
        "Contract drift detected — the SDKs/spec are out of sync with the API:\n"
        f"  live but undocumented: {report.missing_from_spec}\n"
        f"  documented but missing: {report.missing_from_live}"
    )


def test_checker_detects_added_endpoint() -> None:
    live = {("GET", "/api/books"), ("GET", "/api/new/thing")}
    documented = {("GET", "/api/books")}
    report = compute_drift(live, documented)
    assert not report.in_sync
    assert ("GET", "/api/new/thing") in report.missing_from_spec


def test_checker_detects_removed_endpoint() -> None:
    live = {("GET", "/api/books")}
    documented = {("GET", "/api/books"), ("GET", "/api/gone")}
    report = compute_drift(live, documented)
    assert not report.in_sync
    assert ("GET", "/api/gone") in report.missing_from_live


def test_param_name_differences_are_not_drift() -> None:
    # {id} vs {book_id} is the same shape — must not read as drift.
    live = {("GET", "/api/books/{book_id}/shots")}
    documented = {("GET", "/api/books/{id}/shots")}
    assert compute_drift(live, documented).in_sync


def test_catalog_loads() -> None:
    documented = load_catalog_endpoints()
    assert len(documented) >= 30
    assert ("POST", "/api/auth/login") in documented


# --- generated-artifact staleness gates ------------------------------------- #

_GENERATORS = [
    "clients/spec/generate.mjs",
    "clients/spec/sync-ts.mjs",
    "clients/spec/sync-py.mjs",
]


@pytest.mark.skipif(_NODE is None, reason="node not available")
@pytest.mark.parametrize("generator", _GENERATORS)
def test_generated_artifact_in_sync(generator: str) -> None:
    """The TS/Py spec modules + openapi.json must match the source-of-truth catalog.

    Each generator supports `--check` (exit 1 if its output is stale). This is the
    gate that keeps the SDKs and the OpenAPI doc honest with `catalog.mjs`.
    """
    assert _NODE is not None
    result = subprocess.run(  # noqa: S603 - trusted in-repo generator
        [_NODE, generator, "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{generator} is stale — re-run `node {generator}`.\n{result.stderr or result.stdout}"
    )
