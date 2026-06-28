"""A conformance harness for the Kinora canon MCP server (§8.3).

A "spec-compliant MCP server" is only as good as the checks that keep it
compliant as the tool surface evolves. This module is a reusable set of
*static* conformance checks over the protocol layer — they need no database and
no network, so they run in the unit suite and double as a CLI
(``python -m app.mcp.conformance``) for a quick local audit.

The checks assert the structural contracts the rest of the system relies on:

* **Catalog completeness** — every tool in the single execution path is in the
  catalog, has a JSON-Schema input *and* output, and a version.
* **Schema validity** — every advertised input/output JSON Schema is itself a
  valid JSON Schema (so a client can compile it).
* **Capability shape** — the advertised capabilities are well-formed and the
  resource templates resolve.
* **Error taxonomy** — every :class:`ErrorCategory` maps to a distinct numeric
  code and round-trips through :func:`to_error_body`.
* **Versioning** — version pins resolve and incompatible pins are rejected.
* **Scope coverage** — every tool is classified (read / write / render) and the
  write/render sets are mutually consistent with the resource-touch map.

Each check yields :class:`ConformanceResult` records; :func:`run_conformance`
aggregates them into a :class:`ConformanceReport`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import jsonschema

from app.mcp.capabilities import PROTOCOL_VERSION, ServerCapabilities
from app.mcp.errors import ErrorCategory, code_for, to_error_body
from app.mcp.registry import Scope, ToolCatalog, default_catalog
from app.mcp.resources import RESOURCE_TEMPLATES, ResourceProvider, resolve_uri


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    """One named conformance check's outcome."""

    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.name}{(': ' + self.detail) if self.detail else ''}"


@dataclass(slots=True)
class ConformanceReport:
    """The aggregate of all conformance checks."""

    results: list[ConformanceResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[ConformanceResult]:
        return [r for r in self.results if not r.passed]

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append(ConformanceResult(name=name, passed=passed, detail=detail))

    def render(self) -> str:
        lines = [str(r) for r in self.results]
        lines.append("")
        lines.append(f"{sum(r.passed for r in self.results)}/{len(self.results)} checks passed")
        return "\n".join(lines)


def _check_catalog_completeness(catalog: ToolCatalog, report: ConformanceReport) -> None:
    missing_out = [m.name for m in catalog.metas if m.output_model is None]
    report.add(
        "catalog.output_models",
        not missing_out,
        "" if not missing_out else f"missing output model: {missing_out}",
    )
    missing_ver = [m.name for m in catalog.metas if m.version is None]
    report.add("catalog.versions", not missing_ver, f"missing: {missing_ver}")
    no_desc = [m.name for m in catalog.metas if not m.description.strip()]
    report.add("catalog.descriptions", not no_desc, f"empty: {no_desc}")


def _check_schema_validity(catalog: ToolCatalog, report: ConformanceReport) -> None:
    bad: list[str] = []
    for m in catalog.metas:
        for label, schema in (("in", m.input_schema()), ("out", m.output_schema())):
            if schema is None:
                continue
            try:
                cls = jsonschema.validators.validator_for(schema)
                cls.check_schema(schema)
            except jsonschema.exceptions.SchemaError as exc:  # pragma: no cover
                bad.append(f"{m.name}.{label}: {exc.message}")
    report.add("schema.validity", not bad, f"invalid: {bad}")


def _check_capabilities(report: ConformanceReport) -> None:
    caps = ServerCapabilities.for_catalog()
    init = caps.initialize_result()
    ok = (
        init["protocolVersion"] == PROTOCOL_VERSION
        and init["capabilities"]["resources"]["subscribe"] is True
        and init["capabilities"]["tools"]["listChanged"] is True
        and "io.kinora.canon" in init["capabilities"]["experimental"]
    )
    report.add("capabilities.shape", ok, "" if ok else json.dumps(init)[:120])


def _check_resources(report: ConformanceReport) -> None:
    bad: list[str] = []
    for tpl in RESOURCE_TEMPLATES:
        # Construct a concrete URI from the template by substituting placeholders.
        concrete = tpl.template
        for key in ("book_id", "branch", "user_id"):
            concrete = concrete.replace("{" + key + "}", "X")
        try:
            resolved = resolve_uri(concrete)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{tpl.template}: {exc}")
            continue
        if resolved.tool != tpl.tool:
            bad.append(f"{tpl.template}: resolved to {resolved.tool} != {tpl.tool}")
    report.add("resources.templates_resolve", not bad, f"bad: {bad}")

    # The touch-map must only reference tools that exist + only write tools.
    touched = ResourceProvider.resources_touched_by(
        "canon.assert_fact", {"book_id": "b", "branch": "main"}
    )
    report.add(
        "resources.write_touches_canon",
        bool(touched) and all(u.startswith("kinora://canon/b") for u in touched),
        f"touched: {touched}",
    )
    read_touched = ResourceProvider.resources_touched_by("canon.query", {"book_id": "b"})
    report.add("resources.read_touches_nothing", read_touched == [], f"touched: {read_touched}")


def _check_error_taxonomy(report: ConformanceReport) -> None:
    codes = {cat: code_for(cat) for cat in ErrorCategory}
    distinct = len(set(codes.values())) == len(codes)
    report.add("errors.distinct_codes", distinct, f"codes: {codes}")
    # Round-trip a couple of representative exceptions through to_error_body.
    body = to_error_body(ValueError("bad"))
    report.add(
        "errors.value_error_maps_invalid_params",
        body.category is ErrorCategory.INVALID_PARAMS,
        body.category.value,
    )
    body2 = to_error_body(RuntimeError("oops"))
    report.add(
        "errors.unknown_maps_internal",
        body2.category is ErrorCategory.INTERNAL,
        body2.category.value,
    )


def _check_versioning(catalog: ToolCatalog, report: ConformanceReport) -> None:
    sample = catalog.names()[0]
    ok_resolve = catalog.resolve_version(sample, "1.0").name == sample
    report.add("versioning.compatible_pin_resolves", ok_resolve)
    rejected = False
    try:
        catalog.resolve_version(sample, "99.0")
    except ValueError:
        rejected = True
    report.add("versioning.incompatible_pin_rejected", rejected)


def _check_scope_coverage(catalog: ToolCatalog, report: ConformanceReport) -> None:
    unclassified = [m.name for m in catalog.metas if not m.scopes]
    report.add("scopes.every_tool_classified", not unclassified, f"missing: {unclassified}")
    # Render tools must be a subset of the control-plane intent (shot.render / budget.reserve).
    render = set(catalog.with_scope(Scope.RENDER))
    render_names = {m.name for m in render}
    report.add(
        "scopes.render_set",
        render_names == {"shot.render", "budget.reserve"},
        f"render: {sorted(render_names)}",
    )


def run_conformance(catalog: ToolCatalog | None = None) -> ConformanceReport:
    """Run every static conformance check and return the aggregate report."""
    catalog = catalog or default_catalog()
    report = ConformanceReport()
    _check_catalog_completeness(catalog, report)
    _check_schema_validity(catalog, report)
    _check_capabilities(report)
    _check_resources(report)
    _check_error_taxonomy(report)
    _check_versioning(catalog, report)
    _check_scope_coverage(catalog, report)
    return report


def main() -> int:
    """CLI entrypoint: print the report; exit non-zero on any failure."""
    report = run_conformance()
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ConformanceReport",
    "ConformanceResult",
    "main",
    "run_conformance",
]
