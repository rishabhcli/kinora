"""Command-line entrypoint for the API-spec tooling (``python -m app.apispec``).

Three subcommands wrap the library so the contract gate is runnable in CI and by
operators without writing glue:

* ``snapshot``  — write the enriched OpenAPI to a golden file (commit it).
* ``diff``      — compare the live enriched spec against a golden file and exit
                  non-zero on a breaking change (the gate). ``--allow-breaking``
                  downgrades to advisory.
* ``generate``  — emit the TypeScript typed client to a file and report renderer
                  route coverage; exits non-zero if a renderer route is uncovered.

Building the app imports the full router set but runs **no** network/infra and
spends nothing (it only introspects routes), consistent with the rest of the
test-safe tooling.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.apispec.contract import ContractReport  # noqa: F401  (re-export convenience)
from app.apispec.diff import diff_specs, load_snapshot, snapshot_spec
from app.apispec.enricher import build_enriched_spec
from app.apispec.tsclient import generate_client, renderer_coverage


def _live_spec() -> dict:
    from app.main import create_app

    app = create_app()
    return build_enriched_spec(app)


def _cmd_snapshot(args: argparse.Namespace) -> int:
    spec = _live_spec()
    text = snapshot_spec(spec)
    Path(args.out).write_text(text, encoding="utf-8")
    paths = len(spec.get("paths", {}))
    print(f"wrote {args.out} ({paths} paths)")
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    golden = load_snapshot(Path(args.golden).read_text(encoding="utf-8"))
    live = _live_spec()
    result = diff_specs(golden, live)
    print(result.summary())
    for change in result.changes:
        print(f"  {change}")
    if result.breaking and not args.allow_breaking:
        print(f"\nCONTRACT GATE FAILED: {len(result.breaking)} breaking change(s).")
        return 1
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    spec = _live_spec()
    client = generate_client(spec)
    Path(args.out).write_text(client.source, encoding="utf-8")
    print(f"wrote {args.out} ({len(client.method_names)} methods)")
    missing = renderer_coverage(client)
    if missing:
        print("UNCOVERED renderer routes:")
        for method, path in missing:
            print(f"  {method} {path}")
        return 1
    print("all renderer routes covered")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.apispec", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="write the golden enriched spec")
    p_snap.add_argument("--out", default="openapi.golden.json")
    p_snap.set_defaults(func=_cmd_snapshot)

    p_diff = sub.add_parser("diff", help="diff live spec against the golden")
    p_diff.add_argument("--golden", default="openapi.golden.json")
    p_diff.add_argument("--allow-breaking", action="store_true")
    p_diff.set_defaults(func=_cmd_diff)

    p_gen = sub.add_parser("generate", help="emit the TypeScript typed client")
    p_gen.add_argument("--out", default="generated-client.ts")
    p_gen.set_defaults(func=_cmd_generate)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - thin CLI shell
    sys.exit(main())
