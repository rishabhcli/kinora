"""Unit tests for the vectorized query engine + planner over the catalog."""

from __future__ import annotations

import pytest

from app.lakehouse.warehouse import aggregate as agg
from app.lakehouse.warehouse import expr as ex
from app.lakehouse.warehouse.batch import RecordBatch
from app.lakehouse.warehouse.catalog import Catalog
from app.lakehouse.warehouse.engine import TableNotRegisteredError, WarehouseQueryEngine
from app.lakehouse.warehouse.logical import JoinType, LogicalPlan, Scan
from app.lakehouse.warehouse.planner import optimize, to_pushdown
from app.lakehouse.warehouse.types import ColumnVector, Field, LogicalType, Schema


def sales_schema() -> Schema:
    return Schema.of(
        Field("region", LogicalType.STRING),
        Field("product", LogicalType.STRING),
        Field("qty", LogicalType.INT64),
        Field("price", LogicalType.FLOAT64),
    )


def make_engine() -> WarehouseQueryEngine:
    cat = Catalog()
    s = sales_schema()
    t = cat.create_table("sales", s, rows_per_group=3)
    rows = [
        ("us", "a", 2, 10.0),
        ("us", "b", 1, 20.0),
        ("eu", "a", 3, 10.0),
        ("eu", "b", 5, 20.0),
        ("us", "a", 1, 10.0),
        ("ap", "c", 4, 5.0),
        (None, "a", 9, 1.0),
    ]
    t.append(
        RecordBatch.from_mapping(
            s,
            {
                "region": ColumnVector.from_pylist(LogicalType.STRING, [r[0] for r in rows]),
                "product": ColumnVector.from_pylist(LogicalType.STRING, [r[1] for r in rows]),
                "qty": ColumnVector.from_pylist(LogicalType.INT64, [r[2] for r in rows]),
                "price": ColumnVector.from_pylist(LogicalType.FLOAT64, [r[3] for r in rows]),
            },
        )
    )
    return WarehouseQueryEngine.from_catalog(cat)


def find_scan(plan: LogicalPlan) -> Scan:
    if isinstance(plan, Scan):
        return plan
    for c in plan.children():
        try:
            return find_scan(c)
        except AssertionError:
            continue
    raise AssertionError("no scan in plan")


def projection_set(scan: Scan) -> set[str]:
    assert scan.projection is not None
    return set(scan.projection)


# -- scan / filter / project ------------------------------------------------ #


def test_scan_all() -> None:
    eng = make_engine()
    res = eng.execute(eng.scan("sales"))
    assert res.num_rows == 7


def test_filter_project() -> None:
    eng = make_engine()
    plan = (
        eng.scan("sales")
        .filter(ex.Comparison(ex.CompareOp.GE, ex.col("qty"), ex.lit(3)))
        .project(
            [
                ("region", ex.col("region")),
                ("rev", ex.Arithmetic(ex.ArithOp.MUL, ex.col("qty"), ex.col("price"))),
            ]
        )
    )
    res = eng.execute(plan)
    revs = sorted(r["rev"] for r in res.rows())
    assert revs == [9.0, 20.0, 30.0, 100.0]  # qty>=3: eu-a(30), eu-b(100), ap-c(20), null-a(9)


def test_filter_excludes_null_comparisons() -> None:
    eng = make_engine()
    plan = eng.scan("sales").filter(ex.Comparison(ex.CompareOp.EQ, ex.col("region"), ex.lit("us")))
    res = eng.execute(plan)
    assert all(r["region"] == "us" for r in res.rows())
    assert res.num_rows == 3


# -- aggregation ------------------------------------------------------------ #


def test_group_by() -> None:
    eng = make_engine()
    plan = eng.scan("sales").aggregate(
        ["region"],
        [agg.sum_(ex.col("qty"), "tot"), agg.avg(ex.col("price"), "avgp"), agg.count_star("n")],
    )
    res = {r["region"]: r for r in eng.execute(plan).rows()}
    assert res["us"]["tot"] == 4
    assert res["us"]["n"] == 3
    assert res["eu"]["tot"] == 8
    assert res[None]["tot"] == 9  # NULL group preserved


def test_global_aggregate() -> None:
    eng = make_engine()
    plan = eng.scan("sales").aggregate(
        [],
        [
            agg.count_star("total"),
            agg.sum_(ex.col("qty"), "sq"),
            agg.max_(ex.col("price"), "mx"),
        ],
    )
    res = eng.execute(plan).rows()
    assert len(res) == 1
    assert res[0]["total"] == 7
    assert res[0]["sq"] == 25
    assert res[0]["mx"] == 20.0


def test_count_distinct_in_engine() -> None:
    eng = make_engine()
    plan = eng.scan("sales").aggregate([], [agg.count_distinct(ex.col("region"), "regions")])
    # distinct regions: us, eu, ap (NULL not counted)
    assert eng.execute(plan).rows()[0]["regions"] == 3


def test_aggregate_over_empty_input() -> None:
    eng = make_engine()
    plan = eng.scan("sales").filter(
        ex.Comparison(ex.CompareOp.GT, ex.col("qty"), ex.lit(10_000))
    ).aggregate([], [agg.count_star("n"), agg.sum_(ex.col("qty"), "s")])
    res = eng.execute(plan).rows()
    assert res[0]["n"] == 0
    assert res[0]["s"] is None  # SUM over no rows is NULL


# -- sort / limit ----------------------------------------------------------- #


def test_sort_desc_nulls_last() -> None:
    eng = make_engine()
    res = eng.execute(eng.scan("sales").sort([("region", True)]))
    regions = [r["region"] for r in res.rows()]
    assert regions[-1] is None


def test_sort_asc_nulls_last() -> None:
    eng = make_engine()
    res = eng.execute(eng.scan("sales").sort([("region", False)]))
    regions = [r["region"] for r in res.rows()]
    assert regions[-1] is None
    assert regions[0] == "ap"


def test_multi_key_sort() -> None:
    eng = make_engine()
    res = eng.execute(eng.scan("sales").sort([("product", False), ("qty", True)]))
    rows = res.rows()
    # product ascending; within 'a', qty descending: 9, 3, 2, 1
    a_rows = [r["qty"] for r in rows if r["product"] == "a"]
    assert a_rows == [9, 3, 2, 1]


def test_limit_offset() -> None:
    eng = make_engine()
    res = eng.execute(eng.scan("sales").sort([("qty", True)]).limit(2, offset=1))
    assert [r["qty"] for r in res.rows()] == [5, 4]


def test_limit_zero() -> None:
    eng = make_engine()
    res = eng.execute(eng.scan("sales").limit(0))
    assert res.num_rows == 0


# -- joins ------------------------------------------------------------------ #


def make_engine_with_dim() -> WarehouseQueryEngine:
    eng = make_engine()
    cat = Catalog()
    dim = Schema.of(Field("region", LogicalType.STRING), Field("mgr", LogicalType.STRING))
    d = cat.create_table("dim", dim)
    d.append(
        RecordBatch.from_mapping(
            dim,
            {
                "region": ColumnVector.from_pylist(LogicalType.STRING, ["us", "eu"]),
                "mgr": ColumnVector.from_pylist(LogicalType.STRING, ["Alice", "Bob"]),
            },
        )
    )
    eng.register(d)
    return eng


def test_inner_join() -> None:
    eng = make_engine_with_dim()
    plan = eng.scan("sales").join(eng.scan("dim"), [("region", "region")], how=JoinType.INNER)
    rows = eng.execute(plan).rows()
    # us(3) + eu(2) = 5 matching rows; ap and NULL drop.
    assert len(rows) == 5
    assert all("mgr" in r for r in rows)
    assert {r["mgr"] for r in rows} == {"Alice", "Bob"}


def test_left_join_keeps_unmatched() -> None:
    eng = make_engine_with_dim()
    plan = eng.scan("sales").join(eng.scan("dim"), [("region", "region")], how=JoinType.LEFT)
    rows = eng.execute(plan).rows()
    assert len(rows) == 7  # all sales rows
    unmatched = [r for r in rows if r["mgr"] is None]
    assert len(unmatched) == 2  # ap + NULL region


def test_join_renames_clashing_columns() -> None:
    eng = make_engine_with_dim()
    plan = eng.scan("sales").join(eng.scan("dim"), [("region", "region")])
    schema = plan.output_schema()
    assert "right_region" in schema.names


# -- optimisation ----------------------------------------------------------- #


def test_predicate_pushdown_into_scan() -> None:
    eng = make_engine()
    plan = eng.scan("sales").filter(ex.Comparison(ex.CompareOp.GE, ex.col("qty"), ex.lit(3)))
    opt = optimize(plan)
    scan = find_scan(opt)
    assert scan.predicate is not None
    assert "qty" in scan.predicate.columns()


def test_projection_pushdown_into_scan() -> None:
    eng = make_engine()
    plan = eng.scan("sales").project([("r", ex.col("region")), ("q", ex.col("qty"))])
    opt = optimize(plan)
    scan = find_scan(opt)
    assert projection_set(scan) == {"region", "qty"}  # product, price dropped


def test_projection_pushdown_keeps_filter_columns() -> None:
    eng = make_engine()
    plan = (
        eng.scan("sales")
        .filter(ex.Comparison(ex.CompareOp.GE, ex.col("qty"), ex.lit(2)))
        .project([("region", ex.col("region"))])
    )
    opt = optimize(plan)
    scan = find_scan(opt)
    # region (output) + qty (filter) must both be read.
    assert {"region", "qty"} <= projection_set(scan)


def test_to_pushdown_conversion() -> None:
    pred = to_pushdown(ex.Comparison(ex.CompareOp.GT, ex.col("x"), ex.lit(5)))
    assert pred is not None
    # literal-on-left flips the operator.
    flipped = to_pushdown(ex.Comparison(ex.CompareOp.LT, ex.lit(5), ex.col("x")))
    assert flipped is not None
    # OR is not convertible.
    assert to_pushdown(ex.or_(ex.Comparison(ex.CompareOp.GT, ex.col("x"), ex.lit(5)),
                             ex.Comparison(ex.CompareOp.LT, ex.col("x"), ex.lit(1)))) is None


def test_optimize_result_equivalent() -> None:
    eng = make_engine()
    plan = eng.scan("sales").filter(ex.Comparison(ex.CompareOp.GE, ex.col("qty"), ex.lit(3)))
    opt = eng.execute(plan, optimize_plan=True)
    raw = eng.execute(plan, optimize_plan=False)
    assert sorted(opt.column("qty").to_pylist()) == sorted(raw.column("qty").to_pylist())


# -- error handling --------------------------------------------------------- #


def test_unregistered_table() -> None:
    eng = make_engine()
    with pytest.raises(TableNotRegisteredError):
        eng.scan("ghost")


def test_execute_rejects_non_plan() -> None:
    eng = make_engine()
    with pytest.raises(TypeError):
        eng.execute("not a plan")


def test_chained_query_complex() -> None:
    """filter -> group-by -> sort -> limit, end to end."""
    eng = make_engine()
    plan = (
        eng.scan("sales")
        .filter(ex.Comparison(ex.CompareOp.NE, ex.col("region"), ex.lit("ap")))
        .aggregate(["region"], [agg.sum_(ex.col("qty"), "tot")])
        .sort([("tot", True)])
        .limit(2)
    )
    rows = eng.execute(plan).rows()
    assert len(rows) == 2
    # Highest tot first: eu(8) then us(4). NULL region excluded by != 'ap'? No —
    # region != 'ap' is NULL for the NULL row, so it's filtered out.
    assert rows[0]["region"] == "eu"
    assert rows[0]["tot"] == 8
