"""End-to-end: the staged pipeline + the DatasetService façade (no infra)."""

from __future__ import annotations

from app.mlplatform.datasets.contracts import Split
from app.mlplatform.datasets.export import ExportShape
from app.mlplatform.datasets.pipeline import BuildConfig, DatasetPipeline
from app.mlplatform.datasets.service import DatasetService
from app.mlplatform.datasets.splitting import SplitConfig, SplitRatios
from app.mlplatform.datasets.versioning import Operation
from tests.mlplatform.factories import corpus, raw


def _cfg() -> BuildConfig:
    return BuildConfig(split=SplitConfig(ratios=SplitRatios(0.7, 0.15, 0.15)))


def test_full_pipeline_records_lineage() -> None:
    pipe = DatasetPipeline()
    res = pipe.build("crew", corpus(60), config=_cfg())
    ops = [v.operation for v in res.versions]
    assert ops == [
        Operation.INGEST,
        Operation.SCRUB,
        Operation.DEDUP,
        Operation.LABEL,
        Operation.SPLIT,
    ]
    # each version's parent is the previous
    for child, parent in zip(res.versions[1:], res.versions[:-1], strict=True):
        assert child.parents == (parent.version_id,)


def test_pipeline_scrubs_all_pii() -> None:
    res = DatasetPipeline().build("crew", corpus(40), config=_cfg())
    for ex in res.dataset.examples:
        assert "@mail.com" not in ex.input.get("page_text", "")
        assert ex.scrubbed


def test_pipeline_split_is_leak_free() -> None:
    res = DatasetPipeline().build("crew", corpus(60), config=_cfg())
    assert res.split_report is not None
    assert res.split_report.leak_free


def test_pipeline_stage_toggles() -> None:
    cfg = BuildConfig(do_scrub=False, do_dedup=False, do_label=False, do_split=False)
    res = DatasetPipeline().build("crew", corpus(10), config=cfg)
    assert [v.operation for v in res.versions] == [Operation.INGEST]
    assert res.scrub_report is None


def test_service_build_export_stats_drift_diff() -> None:
    svc = DatasetService()
    res = svc.build("crew", corpus(60), config=_cfg())
    name = res.final_version.version_id

    # stats
    st = svc.stats(name)
    assert st.n == res.final_version.n

    # export per split + shape
    sft = svc.export_jsonl("crew", shape=ExportShape.SFT, split=Split.TRAIN)
    assert sft  # non-empty
    pref = svc.export_jsonl("crew", shape=ExportShape.PREFERENCE)
    assert pref

    # lineage
    walk = svc.lineage("crew")
    assert [n.operation for n in walk][0] == "ingest"

    # build summary card
    summary = svc.build_summary("crew")
    assert summary["n"] == res.final_version.n
    assert summary["latest_version"] == name


def test_service_drift_between_two_builds() -> None:
    svc = DatasetService()
    svc.build("v1", corpus(40, books=10), config=_cfg())
    # a corpus that's entirely the adapter role would drift; reuse a skewed source
    from app.mlplatform.datasets.sources import InMemoryTraceSource

    skewed = InMemoryTraceSource()
    for i in range(40):
        skewed.add(raw(f"x{i}", prompt_key="critic.qa", book_id=f"bk{i % 10}", minutes=i))
    svc.build("v2", skewed, config=_cfg())
    report = svc.drift("v1", "v2")
    assert report.overall.value in {"moderate", "significant"}


def test_service_determinism() -> None:
    a = DatasetService().build("crew", corpus(30), config=_cfg())
    b = DatasetService().build("crew", corpus(30), config=_cfg())
    assert a.final_version.content_hash == b.final_version.content_hash


def test_training_feed_helper() -> None:
    svc = DatasetService()
    svc.build("crew", corpus(40), config=_cfg())
    feed = svc.training_feed("crew", shape=ExportShape.SFT, split=Split.TRAIN)
    # every line is valid JSON
    import json

    for line in feed.splitlines():
        if line.strip():
            json.loads(line)


def test_service_derive_golden_extends_lineage() -> None:
    svc = DatasetService()
    res = svc.build("crew", corpus(60), config=_cfg())
    golden = svc.derive_golden("crew")
    assert golden.operation.value == "filter"
    assert golden.parents == (res.final_version.version_id,)
    assert golden.n <= res.final_version.n
    ops = [n.operation for n in svc.lineage(golden.version_id)]
    assert ops == ["ingest", "scrub", "dedup", "label", "split", "filter"]


def test_service_derive_balanced() -> None:
    from app.mlplatform.datasets.sampling import BalanceMode, role_key

    svc = DatasetService()
    svc.build("crew", corpus(60), config=_cfg())
    balanced = svc.derive_balanced("crew", role_key, mode=BalanceMode.UNDERSAMPLE)
    # the two roles should now be evenly represented
    roles: dict[str, int] = {}
    for ex in balanced.dataset.examples:
        roles[ex.role.value] = roles.get(ex.role.value, 0) + 1
    assert len(set(roles.values())) == 1


def test_service_derive_filtered_custom_predicate() -> None:
    from app.mlplatform.datasets.filtering import min_reward

    svc = DatasetService()
    svc.build("crew", corpus(60), config=_cfg())
    filtered = svc.derive_filtered("crew", min_reward(0.8), name="high_reward")
    assert all(
        e.reward is not None and e.reward >= 0.8 for e in filtered.dataset.examples
    )
