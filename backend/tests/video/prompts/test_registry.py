"""Tests for the dialect registry: built-ins, alias resolution, fallback, override."""

from __future__ import annotations

import pytest

from app.video.prompts.base import DialectSpec, PromptDialect
from app.video.prompts.canonical import ShotDescription
from app.video.prompts.registry import (
    FALLBACK_DIALECT,
    DialectRegistry,
    build_default_registry,
    default_registry,
    get_dialect,
    render_for,
)

_BUILTINS = {"wan", "runway", "pika", "kling", "luma", "veo", "sora", "generic"}


def test_default_registry_has_every_builtin() -> None:
    reg = build_default_registry()
    assert set(reg.names()) == _BUILTINS


def test_default_registry_is_singleton() -> None:
    assert default_registry() is default_registry()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("wan", "wan"),
        ("wan2.1-t2v-turbo", "wan"),
        ("dashscope", "wan"),
        ("gen3", "runway"),
        ("gen-3", "runway"),
        ("runwayml", "runway"),
        ("dream-machine", "luma"),
        ("ray-2", "luma"),
        ("kuaishou", "kling"),
        ("google-veo", "veo"),
        ("veo-3", "veo"),
        ("openai-sora", "sora"),
        ("VEO-3", "veo"),  # case-insensitive
        ("  Wan  ", "wan"),  # whitespace-insensitive
    ],
)
def test_alias_resolution(model: str, expected: str) -> None:
    assert build_default_registry().resolve_name(model) == expected


def test_unknown_model_falls_back_to_generic() -> None:
    reg = build_default_registry()
    assert reg.resolve_name("totally-unknown-model-xyz") == FALLBACK_DIALECT
    assert reg.get("totally-unknown-model-xyz").name == "generic"


def test_models_without_dedicated_dialect_map_to_generic() -> None:
    reg = build_default_registry()
    for model in ("minimax", "hailuo", "mochi", "cogvideo"):
        assert reg.resolve_name(model) == "generic"


def test_has_is_alias_aware() -> None:
    reg = build_default_registry()
    assert reg.has("wan2.1-t2v-turbo")
    assert reg.has("WAN")
    assert not reg.has("nonexistent-xyz")


def test_get_dialect_and_render_for_use_default_registry() -> None:
    assert get_dialect("wan").name == "wan"
    out = render_for("gen3", ShotDescription(subject="x", action="acts"))
    assert out.dialect == "runway"


def test_registry_render_convenience_matches_get_then_render() -> None:
    reg = build_default_registry()
    shot = ShotDescription(subject="a knight", action="rides")
    assert reg.render("veo", shot) == reg.get("veo").render(shot)


class _FakeDialect(PromptDialect):
    spec = DialectSpec(name="wan", label="fake override")

    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        return ["OVERRIDDEN"]

    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        return []


def test_register_overrides_existing_name_last_wins() -> None:
    reg = build_default_registry()
    reg.register(_FakeDialect())
    out = reg.render("wan", ShotDescription(subject="x", action="y"))
    assert out.prompt == "OVERRIDDEN"


def test_register_with_aliases() -> None:
    reg = DialectRegistry()
    reg.register(_FakeDialect(), aliases=("myalias",))
    assert reg.resolve_name("myalias") == "wan"


def test_names_are_sorted() -> None:
    names = build_default_registry().names()
    assert names == sorted(names)
