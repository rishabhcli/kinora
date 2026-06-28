#!/usr/bin/env python3
"""Contract-drift checker for the Kinora SDKs.

Re-derives the live REST surface from the FastAPI route modules under
``backend/app/api/routes/*.py`` and diffs it against the single source-of-truth
catalog (``clients/spec/catalog.mjs``, surfaced to Python as
``clients/python/src/kinora/spec.py``). It flags when the backend grows/changes
endpoints that the SDKs + spec have not caught up with — the early-warning that
the clients have fallen behind the API.

Two derivation modes:

  * **static** (default, no backend install needed): parse the route files'
    AST for ``APIRouter(prefix=...)`` and ``@router.<method>("...")`` /
    ``@router.websocket(...)`` decorators, reconstruct each ``/api``-prefixed
    path + method.
  * **dynamic** (``--dynamic``): import ``app.main.create_app`` and read the real
    ``app.routes`` table. More authoritative, but needs the backend importable
    (works with ``DASHSCOPE_API_KEY=test`` and no network).

Exit code 0 = in sync, 1 = drift (with a human-readable diff). Run it in CI to
fail the build when the SDKs lag the API.

    python clients/contract-drift/check_drift.py
    python clients/contract-drift/check_drift.py --dynamic
    python clients/contract-drift/check_drift.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
ROUTES_DIR = REPO_ROOT / "backend" / "app" / "api" / "routes"
SPEC_PY = REPO_ROOT / "clients" / "python" / "src" / "kinora" / "spec.py"
CATALOG_MJS = REPO_ROOT / "clients" / "spec" / "catalog.mjs"

API_PREFIX = "/api"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Routes the catalog deliberately omits from its endpoint table (documented
# elsewhere or not part of the public SDK surface). They must NOT count as drift.
KNOWN_OMISSIONS: set[tuple[str, str]] = {
    # The WebSocket route is documented under WEBSOCKET, not ENDPOINTS.
    ("WS", "/api/ws/sessions/{session_id}"),
}


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str

    def key(self) -> tuple[str, str]:
        return (self.method.upper(), self.path)


# --------------------------------------------------------------------------- #
# 1. The documented surface (from the source-of-truth catalog)
# --------------------------------------------------------------------------- #


def load_catalog_endpoints() -> set[tuple[str, str]]:
    """Read the documented endpoints from the generated Python spec module."""
    namespace: dict[str, object] = {}
    source = SPEC_PY.read_text()
    exec(compile(source, str(SPEC_PY), "exec"), namespace)  # noqa: S102 - trusted generated file
    endpoints = namespace["ENDPOINTS"]
    out: set[tuple[str, str]] = set()
    assert isinstance(endpoints, list)
    for e in endpoints:
        assert isinstance(e, dict)
        out.add((str(e["method"]).upper(), f"{API_PREFIX}{e['path']}"))
    return out


# --------------------------------------------------------------------------- #
# 2. The live surface (static AST parse of the route files)
# --------------------------------------------------------------------------- #


def _router_prefix(tree: ast.Module) -> str:
    """Find the ``APIRouter(prefix=...)`` prefix in a route module (or '')."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_api_router(node.func):
            for kw in node.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
    return ""


def _is_api_router(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "APIRouter") or (
        isinstance(func, ast.Attribute) and func.attr == "APIRouter"
    )


def _route_decorators(tree: ast.Module) -> list[tuple[str, str]]:
    """Extract (method, path) from every ``@router.<method>("...")`` decorator."""
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not isinstance(func, ast.Attribute):
                continue
            # router.get / router.post / ... / router.websocket
            attr = func.attr
            if attr in HTTP_METHODS:
                method = attr.upper()
            elif attr == "websocket":
                method = "WS"
            else:
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "router"):
                continue
            if dec.args and isinstance(dec.args[0], ast.Constant):
                out.append((method, str(dec.args[0].value)))
    return out


def derive_live_endpoints_static() -> set[tuple[str, str]]:
    """Parse every route module statically into the set of (method, full_path)."""
    if not ROUTES_DIR.is_dir():
        raise SystemExit(f"route dir not found: {ROUTES_DIR}")
    out: set[tuple[str, str]] = set()
    for path in sorted(ROUTES_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        prefix = _router_prefix(tree)
        for method, route_path in _route_decorators(tree):
            # The router is mounted under /api; an empty decorator path ("") maps
            # to the prefix itself (e.g. POST /api/books).
            full = f"{API_PREFIX}{prefix}{route_path}"
            full = full.rstrip("/") if full != API_PREFIX else full
            out.add((method, full))
    return out


def derive_live_endpoints_dynamic() -> set[tuple[str, str]]:
    """Import the FastAPI app and read its real route table (authoritative)."""
    backend = REPO_ROOT / "backend"
    sys.path.insert(0, str(backend))
    import os

    os.environ.setdefault("DASHSCOPE_API_KEY", "test")
    from app.main import create_app  # type: ignore[import-not-found]

    app = create_app()
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str) or not path.startswith(API_PREFIX):
            continue
        methods = getattr(route, "methods", None)
        if methods:
            for m in methods:
                if m not in {"HEAD", "OPTIONS"}:
                    out.add((m, path))
        else:
            # A WebSocket route has no `methods`.
            out.add(("WS", path))
    return out


# --------------------------------------------------------------------------- #
# 3. The diff
# --------------------------------------------------------------------------- #


def _normalize(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Normalise path-param names so {id} vs {book_id} never reads as drift.

    The catalog and the routes may name a path parameter differently; what
    matters for contract drift is the *shape* (method + segment positions), not
    the param's spelling. We collapse every ``{...}`` to ``{}``.
    """
    out: set[tuple[str, str]] = set()
    for method, path in pairs:
        out.add((method, re.sub(r"\{[^}]+\}", "{}", path)))
    return out


@dataclass
class DriftReport:
    missing_from_spec: list[tuple[str, str]]  # live but not documented
    missing_from_live: list[tuple[str, str]]  # documented but not in routes

    @property
    def in_sync(self) -> bool:
        return not self.missing_from_spec and not self.missing_from_live

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "missing_from_spec": [f"{m} {p}" for m, p in self.missing_from_spec],
            "missing_from_live": [f"{m} {p}" for m, p in self.missing_from_live],
        }


def compute_drift(live: set[tuple[str, str]], documented: set[tuple[str, str]]) -> DriftReport:
    omissions = _normalize(KNOWN_OMISSIONS)
    live_n = _normalize(live) - omissions
    doc_n = _normalize(documented)
    missing_from_spec = sorted(live_n - doc_n)
    missing_from_live = sorted(doc_n - live_n)
    return DriftReport(missing_from_spec=missing_from_spec, missing_from_live=missing_from_live)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kinora SDK contract-drift checker")
    parser.add_argument("--dynamic", action="store_true", help="import the FastAPI app instead of static parse")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    documented = load_catalog_endpoints()
    live = derive_live_endpoints_dynamic() if args.dynamic else derive_live_endpoints_static()
    report = compute_drift(live, documented)

    if args.json:
        print(json.dumps({"in_sync": report.in_sync, **report.to_dict()}, indent=2))
    else:
        mode = "dynamic" if args.dynamic else "static"
        print(f"Kinora contract-drift check ({mode}): {len(live)} live, {len(documented)} documented")
        if report.in_sync:
            print("OK — the SDKs + spec match the live API surface.")
        else:
            if report.missing_from_spec:
                print("\nDRIFT: live endpoints NOT in the spec/SDK (add them to catalog.mjs):")
                for m, p in report.missing_from_spec:
                    print(f"  + {m} {p}")
            if report.missing_from_live:
                print("\nDRIFT: spec endpoints NOT found in the live routes (stale — remove or fix):")
                for m, p in report.missing_from_live:
                    print(f"  - {m} {p}")
    return 0 if report.in_sync else 1


if __name__ == "__main__":
    raise SystemExit(main())
