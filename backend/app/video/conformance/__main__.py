"""CLI: ``python -m app.video.conformance <provider_id>``.

Runs the golden contract suite against a named provider and prints a human
report. The exit code is the verdict — ``0`` when the adapter conforms, ``1``
when any executed check fails — so the command drops straight into a CI gate or
a deployment preflight.

Provider ids resolve, in order:

1. The built-in reference fake ``"reference"`` (always conformant) — useful as a
   self-test that the harness itself is wired up.
2. Any broken fake from :data:`~app.video.conformance.fakes.BROKEN_BEHAVIOURS`
   (e.g. ``"broken-taxonomy"``) — useful for demoing exactly which check a given
   defect trips.
3. A registered real adapter, when one has opted in via
   :func:`register_provider` (real hosted providers register a fake-transport
   wrapper here so the CLI never touches the network or spends video seconds).

The CLI **never** enables live video. Every built-in target runs against the
deterministic scripted transport; the spend-gate check is exercised with the
gate explicitly closed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable

from .fakes import BROKEN_BEHAVIOURS, FakeKit, fake_kit
from .report import ConformanceReport
from .runner import run_conformance

#: A CLI target builder returns a :class:`FakeKit` (provider + factories) the
#: runner can drive. Real adapters register a fake-transport wrapper here.
TargetBuilder = Callable[[], FakeKit]


def _reference_target() -> FakeKit:
    return fake_kit(name="reference")


def _broken_target(name: str) -> TargetBuilder:
    behaviour = BROKEN_BEHAVIOURS[name]
    return lambda: fake_kit(name=name, behaviour=behaviour)


#: Built-in, network-free targets. Real adapters extend this via register_provider.
_REGISTRY: dict[str, TargetBuilder] = {
    "reference": _reference_target,
    **{name: _broken_target(name) for name in BROKEN_BEHAVIOURS},
}


def register_provider(provider_id: str, builder: TargetBuilder) -> None:
    """Register a CLI-runnable target (a fake-transport wrapper for an adapter).

    Real adapters call this at import time with a builder that constructs the
    adapter bound to a scripted transport, so ``python -m app.video.conformance
    <id>`` can verify them with no network and no spend.
    """
    _REGISTRY[provider_id] = builder


def available_targets() -> list[str]:
    """Every provider id the CLI can run, sorted for stable help output."""
    return sorted(_REGISTRY)


async def _run(provider_id: str) -> ConformanceReport:
    builder = _REGISTRY[provider_id]
    kit = builder()
    return await run_conformance(
        kit.provider, rebuild=kit.rebuild, rebuild_gated=kit.rebuild_gated
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code (0 pass / 1 fail / 2 usage)."""
    parser = argparse.ArgumentParser(
        prog="python -m app.video.conformance",
        description="Run the golden video-adapter conformance suite.",
    )
    parser.add_argument(
        "provider_id",
        nargs="?",
        help="Provider to test (default: list available targets).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available provider ids and exit.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the one-line verdict, not the per-check report.",
    )
    args = parser.parse_args(argv)

    if args.list or not args.provider_id:
        print("Available conformance targets:")
        for name in available_targets():
            print(f"  {name}")
        # Listing is informational, not a verdict.
        return 0 if args.list else 2

    if args.provider_id not in _REGISTRY:
        print(
            f"unknown provider {args.provider_id!r}; "
            f"available: {', '.join(available_targets())}",
            file=sys.stderr,
        )
        return 2

    report = asyncio.run(_run(args.provider_id))
    print(report.summary() if args.quiet else report.render_text())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    raise SystemExit(main())


__all__ = [
    "available_targets",
    "main",
    "register_provider",
]
