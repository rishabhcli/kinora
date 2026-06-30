"""RuntimeConfigPlane facade — typed API, audit, subscriptions, snapshot, reload."""

from __future__ import annotations

import pytest

from app.flags.plane.context import FlagContext
from app.flags.plane.errors import KillSwitchViolation, UnknownFlagError
from app.flags.plane.overrides import TargetingRule
from app.flags.plane.plane import RuntimeConfigPlane
from app.flags.plane.registry import FlagRegistry, build_default_registry
from app.flags.plane.resolution import ResolutionSource
from app.flags.plane.spec import FlagSpec, FlagType
from app.flags.plane.subscriptions import ChangeEvent, ChangeKind


class _StubSettings:
    """A minimal Settings stand-in for binding (no pydantic / infra)."""

    kinora_live_video = False
    provider_gateway_enabled = False
    video_backend = "dashscope"
    watermark_low_s = 25.0
    budget_ceiling_usd = 30.0
    analytics_enabled = True
    render_poison_threshold = 3


def _plane() -> RuntimeConfigPlane:
    return RuntimeConfigPlane(build_default_registry())


def test_registry_has_the_expected_kinora_flags() -> None:
    reg = build_default_registry()
    assert "kinora.live_video" in reg
    assert "video.backend" in reg
    assert "provider.gateway_enabled" in reg
    # the live-video gate is a guarded kill-switch
    assert reg.get("kinora.live_video").kill_switch is True
    assert "kinora.live_video" in {s.key for s in reg.kill_switches()}


def test_bind_settings_makes_base_layer_equal_settings() -> None:
    reg = build_default_registry().bind_settings(_StubSettings())  # type: ignore[arg-type]
    plane = RuntimeConfigPlane(reg)
    assert plane.value("video.backend") == "dashscope"
    assert plane.is_enabled("kinora.live_video") is False
    assert plane.get_float("scheduler.watermark_low_s") == 25.0
    assert plane.get_int("render.poison_threshold") == 3


def test_from_settings_helper() -> None:
    plane = RuntimeConfigPlane.from_settings(_StubSettings())  # type: ignore[arg-type]
    assert plane.is_enabled("analytics.enabled") is True


def test_typed_getters_and_defaults_for_unknown_keys() -> None:
    plane = _plane()
    assert plane.is_enabled("does.not.exist") is False
    assert plane.get_int("does.not.exist", default=7) == 7
    assert plane.get_string("does.not.exist", default="x") == "x"
    assert plane.get_float("does.not.exist", default=1.5) == 1.5
    assert plane.get("does.not.exist").source is ResolutionSource.UNKNOWN_FLAG


def test_set_override_changes_resolved_value() -> None:
    plane = _plane()
    assert plane.is_enabled("provider.gateway_enabled") is False
    plane.set_override("provider.gateway_enabled", True, actor="op")
    assert plane.is_enabled("provider.gateway_enabled") is True
    plane.clear_override("provider.gateway_enabled", actor="op")
    assert plane.is_enabled("provider.gateway_enabled") is False


def test_kill_switch_cannot_be_raised_on_write() -> None:
    plane = _plane()
    # Forcing it OFF is fine.
    plane.set_override("kinora.live_video", False, actor="op")
    # Forcing it ON is rejected at write time.
    with pytest.raises(KillSwitchViolation):
        plane.set_override("kinora.live_video", True, actor="op")
    # Via a targeting rule, too.
    with pytest.raises(KillSwitchViolation):
        plane.add_rule(
            "kinora.live_video", TargetingRule(id="r", value=True, cohort="beta")
        )
    # Via a rollout, too.
    with pytest.raises(KillSwitchViolation):
        plane.set_rollout("kinora.live_video", 50.0)
    # And the resolved value is still off.
    assert plane.is_enabled("kinora.live_video") is False


def test_numeric_kill_switch_can_only_be_lowered() -> None:
    plane = RuntimeConfigPlane.from_settings(_StubSettings())  # type: ignore[arg-type]
    plane.set_override("budget.ceiling_usd", 10.0, actor="op")  # lower the cap: OK
    assert plane.get_float("budget.ceiling_usd") == 10.0
    with pytest.raises(KillSwitchViolation):
        plane.set_override("budget.ceiling_usd", 100.0, actor="op")  # raise: rejected


def test_unknown_flag_write_raises() -> None:
    plane = _plane()
    with pytest.raises(UnknownFlagError):
        plane.set_override("ghost.flag", True)


def test_targeting_by_book_user_cohort_provider() -> None:
    plane = _plane()
    plane.add_rule(
        "video.backend",
        TargetingRule(id="beta", value="minimax", cohort="beta"),
    )
    assert plane.get_string("video.backend", FlagContext(cohort="beta")) == "minimax"
    assert plane.get_string("video.backend", FlagContext(cohort="ga")) == "dashscope"
    assert plane.get_string("video.backend") == "dashscope"


def test_change_subscription_fires_on_every_mutation() -> None:
    plane = _plane()
    events: list[ChangeEvent] = []
    unsubscribe = plane.subscribe(events.append)

    plane.set_override("provider.gateway_enabled", True, actor="op")
    plane.add_rule("video.backend", TargetingRule(id="r", value="minimax", cohort="beta"))
    plane.set_rollout("analytics.enabled", 50.0)

    kinds = [e.kind for e in events]
    assert ChangeKind.SET_STATIC in kinds
    assert ChangeKind.ADD_RULE in kinds
    assert ChangeKind.SET_ROLLOUT in kinds
    # versions strictly increase
    versions = [e.version for e in events]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)

    unsubscribe()
    before = len(events)
    plane.clear_override("provider.gateway_enabled")
    assert len(events) == before  # no longer notified after unsubscribe


def test_subscriber_exception_is_isolated() -> None:
    plane = _plane()
    seen: list[ChangeEvent] = []

    def _bad(_: ChangeEvent) -> None:
        raise RuntimeError("boom")

    plane.subscribe(_bad)
    plane.subscribe(seen.append)
    # The bad subscriber must not prevent the good one nor break the write.
    plane.set_override("provider.gateway_enabled", True)
    assert plane.is_enabled("provider.gateway_enabled") is True
    assert len(seen) == 1


def test_audit_trail_records_changes_newest_first() -> None:
    plane = _plane()
    plane.set_override("provider.gateway_enabled", True, actor="alice")
    plane.set_override("analytics.enabled", False, actor="bob")
    history = plane.history()
    assert history[0].actor == "bob"  # newest first
    assert {r.kind for r in history} == {ChangeKind.SET_STATIC}
    assert all(r.summary for r in history)
    # filterable by flag key
    only_gw = plane.history(flag_key="provider.gateway_enabled")
    assert all(r.flag_key == "provider.gateway_enabled" for r in only_gw)


def test_snapshot_resolves_every_flag_for_a_context() -> None:
    plane = _plane()
    plane.add_rule("video.backend", TargetingRule(id="beta", value="minimax", cohort="beta"))
    snap = plane.snapshot(FlagContext(cohort="beta"))
    assert snap["context"]["cohort"] == "beta"
    assert snap["flags"]["video.backend"]["value"] == "minimax"
    assert set(snap["flags"]) == set(plane.registry.keys())
    assert "layer_version" in snap


def test_export_and_import_round_trip() -> None:
    plane = _plane()
    plane.set_override("provider.gateway_enabled", True)
    plane.add_rule("video.backend", TargetingRule(id="beta", value="minimax", cohort="beta"))
    exported = plane.export_overrides()

    # A fresh plane importing the same dict resolves identically.
    fresh = _plane()
    fresh.import_overrides(exported, actor="op")
    assert fresh.is_enabled("provider.gateway_enabled") is True
    assert fresh.get_string("video.backend", FlagContext(cohort="beta")) == "minimax"


def test_import_rejects_a_layer_that_would_raise_a_kill_switch() -> None:
    plane = _plane()
    bad = {
        "version": 0,
        "overlays": {
            "kinora.live_video": {"static": {"value": True}, "rules": [], "rollout": None}
        },
    }
    with pytest.raises(KillSwitchViolation):
        plane.import_overrides(bad)
    # nothing was persisted (all-or-nothing)
    assert plane.is_enabled("kinora.live_video") is False
    assert plane.export_overrides()["overlays"] == {}


def test_import_fires_single_reload_event() -> None:
    plane = _plane()
    events: list[ChangeEvent] = []
    plane.subscribe(events.append)
    plane.set_override("provider.gateway_enabled", True)
    exported = plane.export_overrides()
    events.clear()
    plane.import_overrides(exported)
    assert len(events) == 1
    assert events[0].kind is ChangeKind.RELOAD
    assert events[0].flag_key is None


def test_duplicate_registry_key_rejected() -> None:
    spec = FlagSpec(key="dup", type=FlagType.BOOL, default=False)
    with pytest.raises(ValueError, match="duplicate"):
        FlagRegistry((spec, spec))


def test_clear_flag_reverts_to_base() -> None:
    plane = _plane()
    plane.set_override("provider.gateway_enabled", True)
    plane.add_rule(
        "provider.gateway_enabled", TargetingRule(id="r", value=False, cohort="beta")
    )
    plane.clear_flag("provider.gateway_enabled", actor="op")
    assert plane.is_enabled("provider.gateway_enabled") is False
    assert plane.export_overrides()["overlays"] == {}
