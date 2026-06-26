"""Tests for app.optim.routing — the model router (cheapest model that holds quality).

The router's load-bearing guarantee is **behavior preservation**: with routing disabled (the
default) ``route(site, default)`` returns ``default`` unchanged for every call-site, so wiring it
changes nothing. Savings are opt-in: enable routing and supply explicit per-site overrides.
"""

from __future__ import annotations

from app.optim.routing import (
    SUGGESTED_OVERRIDES,
    ModelRouter,
    models_for_sites,
)


def test_disabled_router_is_identity_for_any_site() -> None:
    router = ModelRouter(enabled=False, overrides={"critic": "cheap-model"})
    # Even with an override present, a disabled router must never change the model.
    assert router.route("critic", "qwen-vl-max") == "qwen-vl-max"
    assert router.route("unknown-site", "qwen3.7-max") == "qwen3.7-max"


def test_enabled_router_applies_override_for_matching_site() -> None:
    router = ModelRouter(enabled=True, overrides={"cinematographer": "qwen3.5-plus"})
    assert router.route("cinematographer", "qwen3.7-max") == "qwen3.5-plus"


def test_enabled_router_keeps_default_when_site_has_no_override() -> None:
    router = ModelRouter(enabled=True, overrides={"cinematographer": "qwen3.5-plus"})
    assert router.route("showrunner", "qwen3.7-max") == "qwen3.7-max"


def test_unknown_site_never_raises() -> None:
    router = ModelRouter(enabled=True, overrides={})
    assert router.route("brand-new-site", "m") == "m"


def test_models_for_sites_maps_defaults_through_the_router() -> None:
    defaults = {"showrunner": "qwen3.7-max", "cinematographer": "qwen3.7-max"}
    router = ModelRouter(enabled=True, overrides={"cinematographer": "qwen3.5-plus"})
    assert models_for_sites(router, defaults) == {
        "showrunner": "qwen3.7-max",
        "cinematographer": "qwen3.5-plus",
    }


def test_from_settings_disabled_by_default() -> None:
    class _S:
        pass

    router = ModelRouter.from_settings(_S())
    assert router.route("critic", "qwen-vl-max") == "qwen-vl-max"


def test_from_settings_reads_enabled_and_overrides_json() -> None:
    class _S:
        optim_routing_enabled = True
        optim_routing_overrides_json = '{"cinematographer": "qwen3.5-plus"}'

    router = ModelRouter.from_settings(_S())
    assert router.route("cinematographer", "qwen3.7-max") == "qwen3.5-plus"


def test_suggested_overrides_are_documented_candidates_not_applied_by_default() -> None:
    # SUGGESTED_OVERRIDES is a documented menu for operators; the default router must NOT apply it.
    assert isinstance(SUGGESTED_OVERRIDES, dict)
    assert SUGGESTED_OVERRIDES  # non-empty: at least one quality-guarded candidate
    default_router = ModelRouter.from_settings(type("S", (), {})())
    for site, cheaper in SUGGESTED_OVERRIDES.items():
        # Default (disabled) router ignores the suggestion entirely.
        assert default_router.route(site, "ORIGINAL") == "ORIGINAL"
        assert isinstance(cheaper, str) and cheaper
