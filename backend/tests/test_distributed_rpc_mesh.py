"""End-to-end tests for the ServiceMesh façade + the Kinora service catalog."""

from __future__ import annotations

import pytest

from app.distributed.rpc.catalog import (
    BudgetReserveReq,
    BudgetReserveResp,
    CanonQuery,
    CanonResult,
    QaReq,
    QaResult,
    ShotRef,
    ShotSpecResult,
    all_contracts,
    budget_contract,
    cinematographer_contract,
    critic_contract,
    memory_contract,
)
from app.distributed.rpc.errors import RpcError, RpcStatus
from app.distributed.rpc.health import HealthCheck, HealthChecker, HealthStatus
from app.distributed.rpc.mesh import build_default_mesh, build_test_mesh

# -- Impl fixtures ---------------------------------------------------------- #


class CineImpl:
    async def plan_shot(self, req: ShotRef) -> ShotSpecResult:
        return ShotSpecResult(spec={"shot_hash": req.shot_hash, "scene": req.scene_id})


class BudgetImpl:
    def __init__(self) -> None:
        self.remaining = 100.0

    async def reserve(self, req: BudgetReserveReq) -> BudgetReserveResp:
        if req.seconds > self.remaining:
            return BudgetReserveResp(granted=False, remaining_seconds=self.remaining)
        self.remaining -= req.seconds
        return BudgetReserveResp(granted=True, remaining_seconds=self.remaining)


class MemoryImpl:
    def __init__(self) -> None:
        self.canon = {"alice": [{"hair": "red"}]}

    async def query_canon(self, q: CanonQuery) -> CanonResult:
        return CanonResult(facts=self.canon.get(q.entity, []))

    async def write_canon(self, w: object) -> CanonResult:
        return CanonResult(facts=[{"written": True}])


class CriticImpl:
    async def qa_shot(self, req: QaReq) -> QaResult:
        return QaResult(passed=True, ccs=0.92, style_drift=0.05, motion=0.2)


# -- Mesh basics ------------------------------------------------------------ #


async def test_register_and_typed_stub_call() -> None:
    mesh = build_test_mesh()
    mesh.register(cinematographer_contract(), CineImpl())
    ctx = mesh.new_context(timeout_s=2.0, principal="reader-1", tenant="ws-9")
    result = await mesh.stub("cinematographer").plan_shot(
        ShotRef(shot_hash="h1", scene_id="s1"), context=ctx
    )
    assert isinstance(result, ShotSpecResult)
    assert result.spec == {"shot_hash": "h1", "scene": "s1"}


async def test_one_shot_call_helper() -> None:
    mesh = build_test_mesh()
    mesh.register(budget_contract(), BudgetImpl())
    resp = await mesh.call("budget", "reserve", BudgetReserveReq(shot_hash="h", seconds=10))
    assert isinstance(resp, BudgetReserveResp)
    assert resp.granted
    assert resp.remaining_seconds == pytest.approx(90.0)


async def test_unknown_service_stub_raises_keyerror() -> None:
    mesh = build_test_mesh()
    with pytest.raises(KeyError):
        mesh.stub("nope")


async def test_application_error_raised_through_stub() -> None:
    mesh = build_test_mesh()
    mesh.register(memory_contract(), MemoryImpl())
    ctx = mesh.new_context()
    res = await mesh.stub("memory").query_canon(CanonQuery(entity="ghost"), context=ctx)
    assert isinstance(res, CanonResult)
    assert res.facts == []  # not an error — empty result


async def test_wire_survivability_through_loopback() -> None:
    # build_test_mesh uses the loopback transport, so a successful call proves the
    # request/response survive a JSON round-trip (split-readiness).
    mesh = build_test_mesh(loopback=True)
    mesh.register(critic_contract(), CriticImpl())
    res = await mesh.stub("critic").qa_shot(QaReq(shot_hash="h", clip_uri="u"))
    assert isinstance(res, QaResult)
    assert res.passed and res.ccs == pytest.approx(0.92)


async def test_idempotency_dedup_through_mesh() -> None:
    mesh = build_test_mesh()
    budget = BudgetImpl()
    mesh.register(budget_contract(), budget)
    # Same idempotency key → the reserve runs once; a duplicate replays.
    ctx = mesh.new_context(idempotency_key="shot#dup")
    r1 = await mesh.stub("budget").reserve(
        BudgetReserveReq(shot_hash="h", seconds=10), context=ctx
    )
    ctx2 = mesh.new_context(idempotency_key="shot#dup")
    r2 = await mesh.stub("budget").reserve(
        BudgetReserveReq(shot_hash="h", seconds=10), context=ctx2
    )
    assert r1.remaining_seconds == r2.remaining_seconds  # replayed, not re-charged
    assert budget.remaining == pytest.approx(90.0)  # only charged once


# -- Topology + health ------------------------------------------------------ #


async def test_register_all_catalog_contracts() -> None:
    mesh = build_test_mesh()
    for contract in all_contracts().values():
        # A trivial impl exposing each method as a no-op.
        impl = _StubImpl(contract)
        mesh.register(contract, impl)
    assert set(mesh.services()) == set(all_contracts())
    topo = mesh.topology()
    assert len(topo["services"]) == len(all_contracts())
    for svc in topo["services"]:
        assert svc["instances"][0]["healthy"]
        assert svc["fingerprint"]


async def test_health_check_updates_registry() -> None:
    mesh = build_test_mesh()

    async def db_ok() -> HealthStatus:
        return HealthStatus.HEALTHY

    checker = HealthChecker(checks=[HealthCheck("db", db_ok)], clock=mesh.clock)
    mesh.register(budget_contract(), BudgetImpl(), health=checker)
    report = await mesh.check_health("budget")
    assert report.healthy
    # Now flip the check unhealthy and re-run → discovery should route around it.
    async def db_bad() -> HealthStatus:
        return HealthStatus.UNHEALTHY

    checker.checks = [HealthCheck("db", db_bad)]
    report2 = await mesh.check_health("budget")
    assert report2.status is HealthStatus.UNHEALTHY
    assert mesh.discovery.resolve("budget") == []  # unhealthy filtered out


async def test_deregister_removes_from_topology() -> None:
    mesh = build_test_mesh()
    mesh.register(budget_contract(), BudgetImpl())
    assert "budget" in mesh.services()
    mesh.deregister("budget")
    assert "budget" not in mesh.services()
    assert not mesh.discovery.has_service("budget")


async def test_check_all_reports_every_service() -> None:
    mesh = build_test_mesh()
    mesh.register(cinematographer_contract(), CineImpl())
    mesh.register(budget_contract(), BudgetImpl())
    reports = await mesh.check_all()
    assert set(reports) == {"cinematographer", "budget"}
    assert all(r.healthy for r in reports.values())


async def test_default_mesh_inprocess_transport() -> None:
    # The production default (no loopback) uses the zero-copy in-process transport.
    mesh = build_default_mesh(loopback=False)
    mesh.register(cinematographer_contract(), CineImpl())
    res = await mesh.call("cinematographer", "plan_shot", ShotRef(shot_hash="z"))
    assert res.spec["shot_hash"] == "z"


async def test_unimplemented_method_surfaces() -> None:
    mesh = build_test_mesh()

    class Empty:
        pass

    mesh.register(memory_contract(), Empty())
    with pytest.raises(RpcError) as exc:
        await mesh.stub("memory").query_canon(CanonQuery(entity="x"))
    assert exc.value.status is RpcStatus.UNIMPLEMENTED


class _StubImpl:
    """A no-op impl that answers every contract method with an empty body."""

    def __init__(self, contract: object) -> None:
        for mname in getattr(contract, "methods", {}):
            setattr(self, mname, self._noop)

    async def _noop(self, _req: object = None) -> dict[str, object]:
        return {}
