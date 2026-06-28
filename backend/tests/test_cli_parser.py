"""Parser-tree + runner unit tests (no infra).

Verifies the argparse tree wires every command/subcommand to a handler and that
the runner translates a :class:`CliError` into the carried exit code without a
traceback. The container is never built here — these are pure argument tests.
"""

from __future__ import annotations

import argparse

import pytest

from app.cli.errors import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE, CliError
from app.cli.main import _run_handler, build_parser
from app.cli.output import Format, Payload


def _parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_every_command_group_present() -> None:
    parser = build_parser()
    # Each top-level command must resolve to a subparser action.
    help_text = parser.format_help()
    for cmd in ("doctor", "books", "budget", "queue", "canon", "users", "render", "maint"):
        assert cmd in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ("doctor",),
        ("books", "list"),
        ("books", "inspect", "b1"),
        ("books", "set-status", "b1", "ready"),
        ("books", "reingest", "b1"),
        ("books", "delete", "b1"),
        ("budget", "report"),
        ("budget", "remaining"),
        ("budget", "ledger"),
        ("budget", "caps"),
        ("budget", "efficiency"),
        ("queue", "stats"),
        ("queue", "inspect", "j1"),
        ("queue", "dlq"),
        ("queue", "replay", "j1"),
        ("queue", "purge-dlq"),
        ("queue", "reap"),
        ("queue", "cancel", "tok"),
        ("canon", "entities", "b1"),
        ("canon", "states", "b1"),
        ("canon", "audit-verify", "b1"),
        ("canon", "branches", "b1"),
        ("canon", "integrity", "b1"),
        ("users", "list"),
        ("users", "inspect", "--id", "u1"),
        ("users", "orphans"),
        ("users", "reassign", "b1", "u2"),
        ("render", "jobs"),
        ("render", "inspect", "j1"),
        ("render", "defects", "b1"),
        ("maint", "census"),
        ("maint", "stuck-imports"),
        ("maint", "cache-audit"),
        ("maint", "embedding-coverage"),
    ],
)
def test_subcommands_wire_a_handler(argv: tuple[str, ...]) -> None:
    args = _parse(*argv)
    assert callable(getattr(args, "func", None)), argv


def test_format_flag_defaults_to_table() -> None:
    assert _parse("doctor").format == "table"
    assert _parse("-f", "json", "doctor").format == "json"


def test_books_list_status_choice_validates() -> None:
    with pytest.raises(SystemExit):
        _parse("books", "list", "--status", "bogus")


def test_queue_cancel_lane_repeatable() -> None:
    args = _parse("queue", "cancel", "tok", "--lane", "committed", "--lane", "speculative")
    assert args.lane == ["committed", "speculative"]


class _FakeContainer:
    async def shutdown(self) -> None:  # pragma: no cover - trivial
        return None


class _FakeCtxManager:
    """Stands in for build_context so the runner needs no real infra."""

    async def __aenter__(self) -> object:
        from app.cli.context import CliContext

        return CliContext(container=_FakeContainer(), fmt=Format.JSON)  # type: ignore[arg-type]

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_runner_translates_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.cli.main as main_mod

    monkeypatch.setattr(main_mod, "build_context", lambda *_a, **_k: _FakeCtxManager())

    async def _raise(_ctx: object, _args: argparse.Namespace) -> Payload:
        raise CliError("nope", exit_code=EXIT_NOT_FOUND)

    args = argparse.Namespace(func=_raise, format="json", quiet=True)
    code = await _run_handler(args, Format.JSON)
    assert code == EXIT_NOT_FOUND


async def test_runner_renders_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import app.cli.main as main_mod

    monkeypatch.setattr(main_mod, "build_context", lambda *_a, **_k: _FakeCtxManager())

    class _Result:
        def render_payload(self) -> Payload:
            return Payload.of({"ok": True})

    async def _ok(_ctx: object, _args: argparse.Namespace) -> _Result:
        return _Result()

    args = argparse.Namespace(func=_ok, format="json", quiet=True)
    code = await _run_handler(args, Format.JSON)
    assert code == EXIT_OK
    assert '"ok": true' in capsys.readouterr().out


def test_main_no_command_prints_help_and_exits_usage(capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli.main import main

    code = main([])
    assert code == EXIT_USAGE
    assert "usage" in capsys.readouterr().err.lower()
