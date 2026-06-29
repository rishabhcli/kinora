"""Unit tests for the contract value objects + the registry (versioning, refs)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.lakehouse.features import (
    DefinitionError,
    Entity,
    FeatureRef,
    FeatureRegistry,
    FeatureService,
    FeatureSource,
    FeatureSpec,
    FeatureView,
    OnDemandFeatureView,
    ReferenceError,
    ValueType,
)
from app.lakehouse.features.registry import request_inputs_for
from app.lakehouse.features.rows import EntityRow

from .conftest import at, book_features_view, user_stats_view

# --------------------------------------------------------------------------- #
# Value-object validation
# --------------------------------------------------------------------------- #


def test_entity_defaults_join_key_to_name_id() -> None:
    assert Entity(name="user").join_key == "user_id"
    assert Entity(name="book", join_key="isbn").join_key == "isbn"


@pytest.mark.parametrize("bad", ["", "1user", "user-id", "user id", "user.id"])
def test_entity_rejects_bad_names(bad: str) -> None:
    with pytest.raises(DefinitionError):
        Entity(name=bad)


def test_feature_view_rejects_empty_and_duplicate_features() -> None:
    with pytest.raises(DefinitionError):
        FeatureView(
            name="v", entities=(Entity(name="u"),), features=(), source=FeatureSource(name="s")
        )
    with pytest.raises(DefinitionError):
        FeatureView(
            name="v",
            entities=(Entity(name="u"),),
            features=(
                FeatureSpec(name="f", dtype=ValueType.INT),
                FeatureSpec(name="f", dtype=ValueType.FLOAT),
            ),
            source=FeatureSource(name="s"),
        )


def test_feature_view_rejects_nonpositive_ttl() -> None:
    with pytest.raises(DefinitionError):
        FeatureView(
            name="v",
            entities=(Entity(name="u"),),
            features=(FeatureSpec(name="f", dtype=ValueType.INT),),
            source=FeatureSource(name="s"),
            ttl=timedelta(0),
        )


def test_feature_ref_parse_and_str() -> None:
    ref = FeatureRef.parse("user_stats:pages_read")
    assert (ref.view, ref.feature, ref.version) == ("user_stats", "pages_read", None)
    assert ref.column == "user_stats__pages_read"
    pinned = FeatureRef.parse("user_stats:pages_read@7")
    assert pinned.version == 7
    assert str(pinned) == "user_stats:pages_read@7"


@pytest.mark.parametrize("bad", ["nocolon", ":feature", "view:", "view"])
def test_feature_ref_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(ReferenceError):
        FeatureRef.parse(bad)


def test_value_type_classification() -> None:
    assert ValueType.FLOAT.is_numeric
    assert ValueType.INT.is_numeric
    assert ValueType.BOOL.is_numeric
    assert ValueType.STRING.is_categorical
    assert not ValueType.FLOAT_VECTOR.is_numeric
    assert not ValueType.STRING.is_numeric


# --------------------------------------------------------------------------- #
# Registry: content-addressed versioning
# --------------------------------------------------------------------------- #


def test_register_is_idempotent_same_version() -> None:
    reg = FeatureRegistry()
    v1 = reg.register_feature_view(user_stats_view())
    v2 = reg.register_feature_view(user_stats_view())
    assert v1.version == v2.version != 0
    assert reg.feature_view_versions("user_stats") == [v1.version]


def test_changing_definition_mints_new_version() -> None:
    reg = FeatureRegistry()
    v1 = reg.register_feature_view(user_stats_view(ttl_minutes=60))
    v2 = reg.register_feature_view(user_stats_view(ttl_minutes=120))
    assert v1.version != v2.version
    assert set(reg.feature_view_versions("user_stats")) == {v1.version, v2.version}
    # "latest" tracks the most recently registered.
    assert reg.get_feature_view("user_stats").version == v2.version
    # Pinned reads still resolve the old version.
    assert reg.get_feature_view("user_stats", version=v1.version).ttl == timedelta(minutes=60)


def test_register_entity_conflict_raises() -> None:
    reg = FeatureRegistry()
    reg.register_entity(Entity(name="user", join_key="user_id"))
    with pytest.raises(DefinitionError):
        reg.register_entity(Entity(name="user", join_key="uid"))


def test_resolve_unknown_view_and_feature() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    with pytest.raises(ReferenceError):
        reg.resolve(FeatureRef.parse("missing:x"))
    with pytest.raises(ReferenceError):
        reg.resolve(FeatureRef.parse("user_stats:missing"))


def test_feature_service_validates_refs_on_register() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    with pytest.raises(ReferenceError):
        reg.register_feature_service(
            FeatureService(name="svc", features=("user_stats:nope",))
        )


def test_views_for_refs_dedups_views() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    reg.register_feature_view(book_features_view())
    views, refs = reg.views_for_refs(
        ["user_stats:pages_read", "user_stats:avg_dwell_s", "book_feats:popularity"]
    )
    assert {v.name for v in views} == {"user_stats", "book_feats"}
    assert len(views) == 2  # user_stats appears once despite two refs
    assert len(refs) == 3


# --------------------------------------------------------------------------- #
# On-demand views
# --------------------------------------------------------------------------- #


def test_on_demand_registration_and_evaluation() -> None:
    reg = FeatureRegistry()
    reg.register_feature_view(user_stats_view())
    odv = OnDemandFeatureView(
        name="derived",
        features=(FeatureSpec(name="dwell_per_page", dtype=ValueType.FLOAT, default=0.0),),
        source_views=("user_stats",),
    )

    def fn(request, upstream):  # type: ignore[no-untyped-def]
        pages = upstream.get("pages_read") or 1
        dwell = upstream.get("avg_dwell_s") or 0.0
        return {"dwell_per_page": dwell / pages}

    reg.register_on_demand_view(odv, fn)
    out = reg.evaluate_on_demand(
        "derived", request={}, upstream={"pages_read": 10, "avg_dwell_s": 50.0}
    )
    assert out == {"dwell_per_page": 5.0}


def test_on_demand_missing_emitted_feature_raises() -> None:
    reg = FeatureRegistry()
    odv = OnDemandFeatureView(
        name="bad",
        features=(FeatureSpec(name="x", dtype=ValueType.FLOAT),),
    )
    reg.register_on_demand_view(odv, lambda req, up: {"y": 1.0})
    with pytest.raises(DefinitionError):
        reg.evaluate_on_demand("bad", request={}, upstream={})


def test_on_demand_unknown_source_view_raises() -> None:
    reg = FeatureRegistry()
    odv = OnDemandFeatureView(
        name="bad",
        features=(FeatureSpec(name="x", dtype=ValueType.FLOAT),),
        source_views=("missing",),
    )
    with pytest.raises(ReferenceError):
        reg.register_on_demand_view(odv, lambda req, up: {"x": 1.0})


def test_request_inputs_for_helper() -> None:
    ent = EntityRow(keys={"user_id": "u1"}, event_timestamp=at(0), request={"now": 5})
    assert request_inputs_for(ent, ["now", "missing"]) == {"now": 5, "missing": None}
