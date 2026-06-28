"""``python -m app.flags.seed`` — persist the canonical Kinora flags/experiments.

A small operator entrypoint that writes :func:`app.flags.defaults.default_flags`
and :func:`app.flags.defaults.default_experiments` into a deployment's flag
store (idempotently — re-running bumps versions but never duplicates, since the
key is the natural id). Use it to bootstrap a fresh environment so the
``live-video`` gate, the render ladder, and the §13 study exist as durable,
auditable definitions instead of ad-hoc env vars / code branches.

This is a real infra-backed tool (opens Postgres/Redis via the composition
container); it is not part of the unit suite. The seed *content* is unit-tested
through :mod:`app.flags.defaults`, and the *write path* through the store
integration tests — this module is just the thin CLI that wires them together.

Flags:
  --skip-existing   do not overwrite a flag/experiment that already exists.
  --dry-run         print what would be written without touching the store.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.composition import Container, build_container
from app.core.logging import configure_logging, get_logger
from app.flags.defaults import default_experiments, default_flags

logger = get_logger("app.flags.seed")


async def seed(
    container: Container, *, skip_existing: bool = False, dry_run: bool = False
) -> dict[str, list[str]]:
    """Persist the canonical flags/experiments; returns what was written/skipped."""
    service = container.flag_service
    written: list[str] = []
    skipped: list[str] = []

    for flag in default_flags():
        existing = await service.get_flag(flag.key)
        if existing is not None and skip_existing:
            skipped.append(f"flag:{flag.key}")
            continue
        if dry_run:
            written.append(f"flag:{flag.key} (dry-run)")
            continue
        await service.upsert_flag(flag, actor="seed")
        written.append(f"flag:{flag.key}")

    for experiment in default_experiments():
        existing_exp = await service.get_experiment(experiment.key)
        if existing_exp is not None and skip_existing:
            skipped.append(f"experiment:{experiment.key}")
            continue
        if dry_run:
            written.append(f"experiment:{experiment.key} (dry-run)")
            continue
        await service.upsert_experiment(experiment, actor="seed")
        written.append(f"experiment:{experiment.key}")

    return {"written": written, "skipped": skipped}


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Seed canonical Kinora feature flags.")
    parser.add_argument("--skip-existing", action="store_true", help="don't overwrite existing")
    parser.add_argument("--dry-run", action="store_true", help="print only; don't write")
    args = parser.parse_args(argv)

    configure_logging()
    container = build_container()
    try:
        result = await seed(
            container, skip_existing=args.skip_existing, dry_run=args.dry_run
        )
    finally:
        await container.shutdown()

    for item in result["written"]:
        logger.info("flags.seed.written", item=item)
    for item in result["skipped"]:
        logger.info("flags.seed.skipped", item=item)
    print(  # noqa: T201 - operator CLI output
        f"seeded {len(result['written'])} definition(s), "
        f"skipped {len(result['skipped'])}"
    )
    return 0


def main() -> None:
    """Console entrypoint."""
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
