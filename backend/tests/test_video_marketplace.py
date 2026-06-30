"""Deterministic, infra-free tests for the video-provider marketplace.

Covers: listing validation, catalog search/filter/rank correctness, onboarding
wizard transitions + gate failures + reversibility, deprecation/sunset lifecycle
+ migration hints, two-model compare, and the read-only API via TestClient.

No network, no DB, no ``KINORA_LIVE_VIDEO``: the whole subsystem is in-memory and
pure. Timestamps in the seed are fixed so ordering assertions are stable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.errors import install_exception_handlers
from app.video.marketplace import (
    Capability,
    CatalogQuery,
    LicenseClass,
    ListingStatus,
    Maturity,
    Modality,
    ModelCatalog,
    ModelListing,
    PricingModel,
    PricingTier,
    RegionAvailability,
    ReputationPointer,
    SampleRef,
)
from app.video.marketplace import api as mp_api
from app.video.marketplace.errors import (
    GateFailedError,
    InvalidTransitionError,
    LifecycleError,
    ListingNotFoundError,
    ListingValidationError,
)
from app.video.marketplace.lifecycle import LifecycleManager
from app.video.marketplace.onboarding import (
    ConformanceReport,
    OnboardingStage,
    OnboardingWizard,
    default_conformance_probe,
)
from app.video.marketplace.seed import seed_catalog, seed_listings
from app.video.marketplace.service import MarketplaceService
from app.video.marketplace.types import Region

_T = datetime(2026, 2, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _active_listing(key: str = "acme/t2v-1", **overrides: object) -> ModelListing:
    base: dict[str, object] = {
        "key": key,
        "provider": "acme",
        "model_id": "t2v-1",
        "display_name": "Acme T2V 1",
        "modalities": (Modality.TEXT_TO_VIDEO,),
        "capabilities": (Capability.HD_720P, Capability.SEED_CONTROL),
        "pricing": (
            PricingTier(name="std", model=PricingModel.PER_SECOND, unit_price_usd=0.05),
        ),
        "license_class": LicenseClass.COMMERCIAL_OK,
        "tos_accepted": True,
        "maturity": Maturity.GA,
        "status": ListingStatus.ACTIVE,
        "created_at": _T,
        "updated_at": _T,
    }
    base.update(overrides)
    return ModelListing(**base)


# --------------------------------------------------------------------------- #
# listing validation
# --------------------------------------------------------------------------- #
class TestListingValidation:
    def test_valid_listing_constructs_and_derives_pricing(self) -> None:
        li = _active_listing()
        assert li.cheapest_per_second_usd == 0.05
        assert li.min_unit_price_usd == 0.05
        assert li.estimate_clip_usd(seconds=5) == pytest.approx(0.25)
        assert li.supports(modality=Modality.TEXT_TO_VIDEO, capability=Capability.HD_720P)
        assert not li.supports(capability=Capability.AUDIO_TRACK)

    def test_key_must_be_provider_slash_model(self) -> None:
        with pytest.raises(ValidationError):
            _active_listing(key="no-slash-here")

    def test_modalities_required_and_deduped(self) -> None:
        with pytest.raises(ValidationError):
            ModelListing(
                key="x/y",
                provider="x",
                model_id="y",
                display_name="Y",
                modalities=(),  # empty -> invalid
            )
        li = ModelListing(
            key="x/y",
            provider="x",
            model_id="y",
            display_name="Y",
            modalities=(Modality.TEXT_TO_VIDEO, Modality.TEXT_TO_VIDEO),
        )
        assert li.modalities == (Modality.TEXT_TO_VIDEO,)

    def test_audio_track_requires_video_output(self) -> None:
        with pytest.raises(ValidationError):
            ModelListing(
                key="x/img",
                provider="x",
                model_id="img",
                display_name="Img",
                modalities=(Modality.TEXT_TO_IMAGE,),
                capabilities=(Capability.AUDIO_TRACK,),
            )

    def test_active_requires_tos_and_pricing(self) -> None:
        with pytest.raises(ValidationError):
            _active_listing(tos_accepted=False)
        with pytest.raises(ValidationError):
            _active_listing(pricing=())

    def test_deprecated_needs_replacement_or_note(self) -> None:
        with pytest.raises(ValidationError):
            _active_listing(
                status=ListingStatus.DEPRECATED, replacement_key=None, migration_note=""
            )
        # naming a replacement is enough
        li = _active_listing(status=ListingStatus.DEPRECATED, replacement_key="acme/t2v-2")
        assert li.status == ListingStatus.DEPRECATED

    def test_pricing_tier_estimate_clamps_and_floors(self) -> None:
        tier = PricingTier(
            name="capped",
            model=PricingModel.PER_SECOND,
            unit_price_usd=0.10,
            max_billable_units=10.0,
            min_charge_usd=0.20,
        )
        assert tier.estimate_usd(units=5) == pytest.approx(0.50)
        assert tier.estimate_usd(units=100) == pytest.approx(1.0)  # clamped to 10s
        assert tier.estimate_usd(units=1) == pytest.approx(0.20)  # floor
        assert tier.estimate_usd(units=-3) == pytest.approx(0.20)  # negative -> floor

    def test_region_availability_serves_and_blocks(self) -> None:
        ra = RegionAvailability(regions=(Region.US, Region.EU), blocked=(Region.EU,))
        assert ra.serves(Region.US)
        assert not ra.serves(Region.EU)  # blocked overrides
        assert not ra.serves(Region.APAC)
        glob = RegionAvailability(regions=(Region.GLOBAL,))
        assert glob.serves(Region.APAC)

    def test_evolve_revalidates_and_bumps_updated_at(self) -> None:
        li = _active_listing()
        evolved = li.evolve(now=_T, summary="changed")
        assert evolved.summary == "changed"
        assert evolved.updated_at == _T
        # evolving into an invalid state raises
        with pytest.raises(ValidationError):
            li.evolve(tos_accepted=False)

    def test_validate_listing_maps_to_typed_error(self) -> None:
        # a hand-corrupted snapshot validated through the typed helper
        from app.video.marketplace.listing import listing_from_manifest

        with pytest.raises(ListingValidationError):
            listing_from_manifest({"key": "bad", "provider": "p"})  # missing required fields


# --------------------------------------------------------------------------- #
# catalog search / filter / rank
# --------------------------------------------------------------------------- #
class TestCatalogSearch:
    def test_seed_catalog_has_expected_keys(self) -> None:
        cat = seed_catalog()
        keys = set(cat.keys())
        assert "dashscope/wan2.1-t2v-turbo" in keys
        assert "minimax/MiniMax-Hailuo-2.3-Fast" in keys
        assert len(seed_listings()) == len(cat)

    def test_empty_query_returns_default_visible_only(self) -> None:
        cat = seed_catalog()
        results = cat.search()
        # retired are hidden by default; seed has none retired, so all visible
        assert all(r.listing.status.is_visible_by_default for r in results)
        assert len(results) == len(cat)

    def test_filter_by_modality(self) -> None:
        cat = seed_catalog()
        i2v = cat.search(CatalogQuery(modality=Modality.IMAGE_TO_VIDEO))
        assert i2v
        assert all(Modality.IMAGE_TO_VIDEO in r.listing.modalities for r in i2v)

    def test_filter_by_capabilities_is_conjunctive(self) -> None:
        cat = seed_catalog()
        q = CatalogQuery(capabilities=(Capability.LONG_DURATION, Capability.FIRST_LAST_FRAME))
        res = cat.search(q)
        assert res
        for r in res:
            assert Capability.LONG_DURATION in r.listing.capabilities
            assert Capability.FIRST_LAST_FRAME in r.listing.capabilities

    def test_filter_by_price_ceiling(self) -> None:
        cat = seed_catalog()
        cheap = cat.search(CatalogQuery(max_price_per_second_usd=0.02))
        assert cheap
        for r in cheap:
            per_s = r.listing.cheapest_per_second_usd
            assert per_s is not None and per_s <= 0.02
        # the cheap MiniMax model (0.018) qualifies; the free research model ($0) too
        assert any(r.listing.key == "minimax/MiniMax-Hailuo-2.3-Fast" for r in cheap)
        assert any(r.listing.key == "labx/openvid-r1" for r in cheap)

    def test_filter_by_min_maturity(self) -> None:
        cat = seed_catalog()
        ga = cat.search(CatalogQuery(min_maturity=Maturity.GA))
        assert ga
        assert all(r.listing.maturity == Maturity.GA for r in ga)

    def test_filter_by_license(self) -> None:
        cat = seed_catalog()
        research = cat.search(CatalogQuery(license_class=LicenseClass.RESEARCH_ONLY))
        assert [r.listing.key for r in research] == ["labx/openvid-r1"]

    def test_filter_by_min_duration(self) -> None:
        cat = seed_catalog()
        longish = cat.search(CatalogQuery(min_duration_s=10.0))
        assert longish
        assert all(r.listing.max_duration_s >= 10.0 for r in longish)

    def test_text_search_matches_tags_and_summary(self) -> None:
        cat = seed_catalog()
        res = cat.search(CatalogQuery(text="budget"))
        assert [r.listing.key for r in res] == ["minimax/MiniMax-Hailuo-2.3-Fast"]

    def test_results_sorted_by_score_then_key(self) -> None:
        cat = seed_catalog()
        res = cat.search()
        scores = [r.score for r in res]
        assert scores == sorted(scores, reverse=True)
        # ties (if any) break by ascending key
        for a, b in zip(res, res[1:], strict=False):
            if a.score == b.score:
                assert a.listing.key <= b.listing.key

    def test_breakdown_sums_to_score(self) -> None:
        cat = seed_catalog()
        for r in cat.search():
            assert r.score == pytest.approx(sum(r.breakdown.values()), abs=1e-6)

    def test_capability_signal_rewards_query_matches(self) -> None:
        # two listings, identical except one has the requested capability
        a = _active_listing(
            key="z/a",
            capabilities=(Capability.HD_720P,),
            reputation=ReputationPointer(source="t", score=0.5, sample_size=1, as_of=_T),
        )
        b = _active_listing(
            key="z/b",
            capabilities=(Capability.HD_720P, Capability.CAMERA_CONTROL),
            reputation=ReputationPointer(source="t", score=0.5, sample_size=1, as_of=_T),
        )
        cat = ModelCatalog([a, b])
        res = cat.search(CatalogQuery(capabilities=(Capability.CAMERA_CONTROL,)))
        # only b advertises CAMERA_CONTROL, so only b passes the filter
        assert [r.listing.key for r in res] == ["z/b"]

    def test_limit_truncates(self) -> None:
        cat = seed_catalog()
        res = cat.search(CatalogQuery(limit=2))
        assert len(res) == 2

    def test_get_missing_raises(self) -> None:
        cat = seed_catalog()
        with pytest.raises(ListingNotFoundError):
            cat.get("nope/nope")


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #
class TestCompare:
    def test_compare_prefers_cheaper_and_more_capable(self) -> None:
        cat = seed_catalog()
        cmp = cat.compare("minimax/MiniMax-Hailuo-2.3-Fast", "dashscope/wan2.5-t2v-preview")
        by_attr = {r.attribute: r for r in cmp.rows}
        # MiniMax (left) is far cheaper per second
        assert by_attr["price_per_second_usd"].prefer == "left"
        # wan2.5 (right) is higher reputation
        assert by_attr["reputation"].prefer == "right"
        assert cmp.recommendation in {"left", "right", "tie"}

    def test_compare_unknown_key_raises(self) -> None:
        cat = seed_catalog()
        with pytest.raises(ListingNotFoundError):
            cat.compare("dashscope/wan2.1-t2v-turbo", "nope/nope")

    def test_compare_price_prefers_priced_over_unpriced(self) -> None:
        priced = _active_listing(key="p/priced")
        unpriced = _active_listing(
            key="p/unpriced",
            pricing=(PricingTier(name="clip", model=PricingModel.PER_CLIP, unit_price_usd=0.5),),
        )
        cat = ModelCatalog([priced, unpriced])
        cmp = cat.compare("p/priced", "p/unpriced")
        row = {r.attribute: r for r in cmp.rows}["price_per_second_usd"]
        # left has per-second pricing; right has none -> left preferred
        assert row.prefer == "left"


# --------------------------------------------------------------------------- #
# onboarding wizard
# --------------------------------------------------------------------------- #
class TestOnboarding:
    def _candidate(self, **overrides: object) -> ModelListing:
        base: dict[str, object] = {
            "key": "newco/great-t2v",
            "provider": "newco",
            "model_id": "great-t2v",
            "display_name": "Great T2V",
            "modalities": (Modality.TEXT_TO_VIDEO,),
            "capabilities": (Capability.HD_720P, Capability.SEED_CONTROL),
            "pricing": (
                PricingTier(name="std", model=PricingModel.PER_SECOND, unit_price_usd=0.06),
            ),
            "license_class": LicenseClass.COMMERCIAL_OK,
            "tos_accepted": True,
            "maturity": Maturity.BETA,
            "status": ListingStatus.DRAFT,
            "created_at": _T,
            "updated_at": _T,
        }
        base.update(overrides)
        return ModelListing(**base)

    def test_happy_path_reaches_active(self) -> None:
        wiz = OnboardingWizard.declare(self._candidate())
        results = wiz.run_all(now=_T)
        assert all(r.passed for r in results)
        assert wiz.stage == OnboardingStage.ACTIVATED
        assert wiz.listing.status == ListingStatus.ACTIVE
        # history is explainable: each gate has reasons
        assert all(r.reasons for r in wiz.history)

    def test_gates_advance_one_stage_each(self) -> None:
        wiz = OnboardingWizard.declare(self._candidate())
        assert wiz.stage == OnboardingStage.DECLARED
        assert wiz.validate_manifest().to_stage == OnboardingStage.MANIFEST_VALID
        assert wiz.validate_capabilities().to_stage == OnboardingStage.CAPABILITIES_VALID
        assert wiz.run_conformance().to_stage == OnboardingStage.CONFORMANCE_PASSED
        assert wiz.configure().to_stage == OnboardingStage.CONFIGURED
        assert wiz.stage_preview(now=_T).to_stage == OnboardingStage.STAGED
        assert wiz.listing.status == ListingStatus.PREVIEW
        assert wiz.activate(now=_T).to_stage == OnboardingStage.ACTIVATED

    def test_out_of_order_gate_raises(self) -> None:
        wiz = OnboardingWizard.declare(self._candidate())
        with pytest.raises(InvalidTransitionError):
            wiz.run_conformance()  # cannot skip manifest/capabilities

    def test_configure_gate_fails_without_pricing(self) -> None:
        # a DRAFT candidate with no pricing
        cand = self._candidate(pricing=())
        wiz = OnboardingWizard.declare(cand)
        wiz.validate_manifest()
        wiz.validate_capabilities()
        wiz.run_conformance()
        res = wiz.configure()
        assert not res.passed
        assert any("pricing" in r for r in res.reasons)
        # still at conformance stage (gate failure does not advance)
        assert wiz.stage == OnboardingStage.CONFORMANCE_PASSED

    def test_configure_requires_commercial_license_when_asked(self) -> None:
        cand = self._candidate(license_class=LicenseClass.RESEARCH_ONLY)
        wiz = OnboardingWizard.declare(cand)
        wiz.validate_manifest()
        wiz.validate_capabilities()
        wiz.run_conformance()
        res = wiz.configure(require_commercial_license=True)
        assert not res.passed
        assert any("commercial" in r for r in res.reasons)

    def test_conformance_failure_blocks(self) -> None:
        # inconsistent: advertises LONG_DURATION but ceiling is 4s -> fatal finding
        cand = self._candidate(
            capabilities=(Capability.LONG_DURATION,),
            max_duration_s=4.0,
        )
        wiz = OnboardingWizard.declare(cand)
        wiz.validate_manifest()
        wiz.validate_capabilities()
        res = wiz.run_conformance()
        assert not res.passed
        assert any("LONG_DURATION" in r or "inconsistent" in r for r in res.reasons)

    def test_advisory_conformance_finding_is_non_blocking(self) -> None:
        # i2v without FIRST_LAST_FRAME/CHARACTER_CONSISTENCY -> advisory only
        cand = self._candidate(
            key="newco/i2v",
            model_id="i2v",
            modalities=(Modality.IMAGE_TO_VIDEO,),
            capabilities=(Capability.HD_720P,),
        )
        report = default_conformance_probe(cand)
        assert report.ok  # advisory does not flip ok
        assert any("advisory" in f for f in report.findings)

    def test_custom_probe_can_fail(self) -> None:
        def strict_probe(_li: ModelListing) -> ConformanceReport:
            return ConformanceReport(
                ok=False,
                checked_modalities=(),
                checked_capabilities=(),
                findings=("simulated protocol mismatch",),
            )

        wiz = OnboardingWizard.declare(self._candidate(), probe=strict_probe)
        wiz.validate_manifest()
        wiz.validate_capabilities()
        res = wiz.run_conformance()
        assert not res.passed
        assert "simulated protocol mismatch" in res.reasons

    def test_revert_is_reversible(self) -> None:
        wiz = OnboardingWizard.declare(self._candidate())
        wiz.run_all(now=_T)
        assert wiz.stage == OnboardingStage.ACTIVATED
        wiz.revert_to(OnboardingStage.STAGED, now=_T)
        assert wiz.stage == OnboardingStage.STAGED
        assert wiz.listing.status == ListingStatus.PREVIEW
        # revert earlier -> back to draft
        wiz.revert_to(OnboardingStage.DECLARED, now=_T)
        assert wiz.stage == OnboardingStage.DECLARED
        assert wiz.listing.status == ListingStatus.DRAFT

    def test_revert_forward_is_rejected(self) -> None:
        wiz = OnboardingWizard.declare(self._candidate())
        wiz.validate_manifest()
        with pytest.raises(InvalidTransitionError):
            wiz.revert_to(OnboardingStage.ACTIVATED)

    def test_require_passed_raises_on_failure(self) -> None:
        cand = self._candidate(pricing=())
        wiz = OnboardingWizard.declare(cand)
        wiz.validate_manifest()
        wiz.validate_capabilities()
        wiz.run_conformance()
        with pytest.raises(GateFailedError):
            wiz.require_passed(wiz.configure())

    def test_service_onboard_only_mutates_catalog_on_success(self) -> None:
        svc = MarketplaceService(ModelCatalog([]))
        # failing candidate (no pricing) -> catalog untouched
        bad = self._candidate(pricing=())
        _wiz, results = svc.onboard(bad)
        assert not all(r.passed for r in results)
        assert len(svc.catalog) == 0
        # good candidate -> upserted as ACTIVE
        good = self._candidate()
        wiz, results = svc.onboard(good)
        assert all(r.passed for r in results)
        assert good.key in svc.catalog
        assert svc.get(good.key).status == ListingStatus.ACTIVE


# --------------------------------------------------------------------------- #
# lifecycle + migration hints
# --------------------------------------------------------------------------- #
class TestLifecycle:
    def test_deprecate_then_migration_hint(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        event = mgr.deprecate(
            "dashscope/wan2.1-t2v-turbo",
            replacement_key="dashscope/wan2.5-t2v-preview",
            migration_note="move to the preview quality tier",
            now=_T,
        )
        assert event.to_status == ListingStatus.DEPRECATED
        assert cat.get("dashscope/wan2.1-t2v-turbo").status == ListingStatus.DEPRECATED

        hint = mgr.migration_hint("dashscope/wan2.1-t2v-turbo")
        assert hint.to_key == "dashscope/wan2.5-t2v-preview"
        assert hint.replacement_available  # preview is selectable
        # the replacement adds 1080p/long-duration/camera over the turbo
        assert Capability.HD_1080P in hint.gained_capabilities
        # and it costs more per second
        assert hint.price_delta_per_second_usd is not None
        assert hint.price_delta_per_second_usd > 0

    def test_seed_ships_a_predeprecated_model_with_hint(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        hint = mgr.migration_hint("dashscope/wan2.2-t2v-plus")
        assert hint.to_key == "dashscope/wan2.5-t2v-preview"
        assert hint.replacement_available
        assert "wan2.5" in (hint.to_key or "")

    def test_deprecate_requires_path(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        with pytest.raises(LifecycleError):
            mgr.deprecate("dashscope/wan2.1-t2v-turbo")  # no replacement/note

    def test_replacement_must_exist(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        with pytest.raises(LifecycleError):
            mgr.deprecate(
                "dashscope/wan2.1-t2v-turbo",
                replacement_key="ghost/none",
            )

    def test_cannot_be_own_replacement(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        with pytest.raises(LifecycleError):
            mgr.deprecate(
                "dashscope/wan2.1-t2v-turbo",
                replacement_key="dashscope/wan2.1-t2v-turbo",
            )

    def test_full_lifecycle_active_to_retired(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        key = "dashscope/wan2.1-i2v-turbo"
        mgr.deprecate(key, replacement_key="minimax/MiniMax-Hailuo-2.3-Fast", now=_T)
        mgr.sunset(key, now=_T)
        assert cat.get(key).status == ListingStatus.SUNSET
        # sunset is no longer selectable
        assert not cat.get(key).status.is_selectable
        mgr.retire(key, now=_T)
        retired = cat.get(key)
        assert retired.status == ListingStatus.RETIRED
        # hidden from default search, visible with include_retired
        assert all(r.listing.key != key for r in cat.search())
        assert any(r.listing.key == key for r in cat.search(CatalogQuery(include_retired=True)))

    def test_illegal_transition_rejected(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        # cannot retire directly from ACTIVE (must deprecate/sunset first)
        with pytest.raises(LifecycleError):
            mgr.retire("dashscope/wan2.1-t2v-turbo")

    def test_migration_hint_dead_end_when_no_replacement(self) -> None:
        cat = seed_catalog()
        mgr = LifecycleManager(cat)
        mgr.deprecate("dashscope/wan2.1-t2v-turbo", migration_note="no successor planned", now=_T)
        hint = mgr.migration_hint("dashscope/wan2.1-t2v-turbo")
        assert hint.to_key is None
        assert not hint.replacement_available
        assert hint.note == "no successor planned"


# --------------------------------------------------------------------------- #
# read-only API via TestClient
# --------------------------------------------------------------------------- #
def _client() -> TestClient:
    # rebuild the singleton from a fresh seed so tests are independent
    mp_api._SERVICE = MarketplaceService(seed_catalog())
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(mp_api.router, prefix="/api")
    return TestClient(app)


class TestApi:
    def test_list_listings(self) -> None:
        resp = _client().get("/api/video/marketplace/listings")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        keys = {row["key"] for row in body}
        assert "dashscope/wan2.1-t2v-turbo" in keys
        # each view carries derived fields
        turbo = next(r for r in body if r["key"] == "dashscope/wan2.1-t2v-turbo")
        assert turbo["cheapest_per_second_usd"] == 0.04
        assert "t2v" in turbo["tags"]

    def test_get_listing_path_key(self) -> None:
        resp = _client().get("/api/video/marketplace/listings/dashscope/wan2.1-t2v-turbo")
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Wan 2.1 T2V Turbo"

    def test_get_missing_listing_404(self) -> None:
        resp = _client().get("/api/video/marketplace/listings/nope/nope")
        assert resp.status_code == 404
        assert resp.json()["error"]["type"] == "listing_not_found"

    def test_search_endpoint_ranks(self) -> None:
        resp = _client().post(
            "/api/video/marketplace/search",
            json={"modality": "image_to_video", "max_price_per_second_usd": 0.06},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert rows
        scores = [r["score"] for r in rows]
        assert scores == sorted(scores, reverse=True)
        for r in rows:
            assert "image_to_video" in r["listing"]["modalities"]
            assert "breakdown" in r

    def test_search_capability_filter(self) -> None:
        resp = _client().post(
            "/api/video/marketplace/search",
            json={"capabilities": ["long_duration", "first_last_frame"]},
        )
        assert resp.status_code == 200
        for r in resp.json():
            caps = r["listing"]["capabilities"]
            assert "long_duration" in caps and "first_last_frame" in caps

    def test_compare_endpoint(self) -> None:
        resp = _client().get(
            "/api/video/marketplace/compare",
            params={
                "left": "minimax/MiniMax-Hailuo-2.3-Fast",
                "right": "dashscope/wan2.5-t2v-preview",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["left_key"] == "minimax/MiniMax-Hailuo-2.3-Fast"
        attrs = {row["attribute"] for row in body["rows"]}
        assert {"price_per_second_usd", "reputation", "capabilities"} <= attrs
        assert body["recommendation"] in {"left", "right", "tie"}

    def test_compare_missing_404(self) -> None:
        resp = _client().get(
            "/api/video/marketplace/compare",
            params={"left": "dashscope/wan2.1-t2v-turbo", "right": "nope/nope"},
        )
        assert resp.status_code == 404

    def test_migration_endpoint_for_predeprecated(self) -> None:
        resp = _client().get(
            "/api/video/marketplace/listings/dashscope/wan2.2-t2v-plus/migration"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["to_key"] == "dashscope/wan2.5-t2v-preview"
        assert body["replacement_available"] is True

    def test_capabilities_vocabulary(self) -> None:
        resp = _client().get("/api/video/marketplace/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        assert "text_to_video" in body["modalities"]
        assert "long_duration" in body["capabilities"]
        assert "ga" in body["maturities"]

    def test_include_retired_query(self) -> None:
        client = _client()
        # deprecate->sunset->retire one through the service behind the singleton
        svc = mp_api.get_service()
        key = "dashscope/wan2.1-i2v-turbo"
        svc.deprecate(key, replacement_key="minimax/MiniMax-Hailuo-2.3-Fast")
        svc.sunset(key)
        svc.retire(key)
        default = {r["key"] for r in client.get("/api/video/marketplace/listings").json()}
        assert "dashscope/wan2.1-i2v-turbo" not in default
        with_retired = {
            r["key"]
            for r in client.get(
                "/api/video/marketplace/listings", params={"include_retired": True}
            ).json()
        }
        assert "dashscope/wan2.1-i2v-turbo" in with_retired

    def test_sample_ref_is_storable(self) -> None:
        # listings carry sample refs; the turbo has one
        client = _client()
        turbo = client.get(
            "/api/video/marketplace/listings/dashscope/wan2.1-t2v-turbo"
        ).json()
        assert turbo["sample_count"] == 1


def test_sample_ref_value_object() -> None:
    s = SampleRef(uri="s3://bucket/clip.mp4", modality=Modality.TEXT_TO_VIDEO, duration_s=5.0)
    assert s.uri.endswith("clip.mp4")
    assert s.modality.is_video_output
