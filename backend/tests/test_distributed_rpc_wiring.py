"""Tests for the composition-root wiring helpers."""

from __future__ import annotations

from app.distributed.rpc.catalog import (
    BudgetReserveReq,
    BudgetReserveResp,
    ShotRef,
    ShotSpecResult,
)
from app.distributed.rpc.health import HealthStatus
from app.distributed.rpc.mesh import build_test_mesh
from app.distributed.rpc.wiring import (
    callable_health_check,
    fresh_mesh_for_process,
    liveness_checker,
    mount_catalog_services,
)


class Cine:
    async def plan_shot(self, req: ShotRef) -> ShotSpecResult:
        return ShotSpecResult(spec={"h": req.shot_hash})


class Budget:
    async def reserve(self, req: BudgetReserveReq) -> BudgetReserveResp:
        return BudgetReserveResp(granted=True, remaining_seconds=42.0)


async def test_mount_only_supplied_impls() -> None:
    mesh = build_test_mesh()
    mounted = mount_catalog_services(mesh, cinematographer=Cine(), budget=Budget())
    assert set(mounted) == {"cinematographer", "budget"}
    assert set(mesh.services()) == {"cinematographer", "budget"}
    # Both are callable through the mesh.
    res = await mesh.call("cinematographer", "plan_shot", ShotRef(shot_hash="x"))
    assert res.spec == {"h": "x"}
    res2 = await mesh.call("budget", "reserve", BudgetReserveReq(shot_hash="x", seconds=1))
    assert res2.granted


async def test_mount_nothing_when_no_impls() -> None:
    mesh = build_test_mesh()
    assert mount_catalog_services(mesh) == []
    assert mesh.services() == []


async def test_liveness_checker_reports_healthy() -> None:
    checker = liveness_checker(name="self")
    report = await checker.run()
    assert report.healthy


async def test_callable_health_check_true_false() -> None:
    async def up() -> bool:
        return True

    async def down() -> bool:
        return False

    assert (await callable_health_check("ok", up).probe()) is HealthStatus.HEALTHY
    assert (await callable_health_check("bad", down).probe()) is HealthStatus.UNHEALTHY


def test_fresh_mesh_for_process_builds() -> None:
    mesh = fresh_mesh_for_process(default_timeout_s=3.0)
    assert mesh.default_timeout_s == 3.0
    assert mesh.services() == []
