"""Prometheus alerting + recording rules generated from the SLO set.

Alerts-as-code: rather than hand-maintaining a ``rules.yml`` that drifts from the
SLO definitions, this module *derives* the rule groups from
:func:`app.telemetry.slo.default_slos`. For each SLO with burn windows it emits a
multi-window/multi-burn-rate alert per tier — the page/ticket ladder from the SRE
workbook — plus convenience recording rules for the error ratios.

The output is a plain dict matching Prometheus' rule-file schema, so it can be
dumped to YAML (``rules_yaml``) and mounted at the Prometheus config path, or
served from the metrics route for the demo/operator. No PyYAML dependency: a tiny
deterministic emitter handles the (simple, known) shape.
"""

from __future__ import annotations

import re
from typing import Any

from app.telemetry.slo import SLO, default_slos

#: Matches a Prometheus range selector (``[5m]`` / ``[15m]`` / ``[1h]`` …) so the
#: SLI's default window can be rewritten to a burn-rate window.
_RANGE_RE = re.compile(r"\[\d+[smhdw]\]")


def _with_window(query: str, window: str) -> str:
    """Rewrite every range selector in ``query`` to ``[window]``."""
    return _RANGE_RE.sub(f"[{window}]", query)


def _error_ratio_expr(slo: SLO, window: str) -> str:
    """A burn-ratio expression: bad-event ratio over ``window`` ÷ error budget."""
    budget = slo.error_budget if slo.error_budget > 0 else 1e-9
    good_ratio = _with_window(slo.sli_query, window)
    # The good-ratio SLI inverted into a bad-ratio, normalized by the error budget.
    return f"(1 - ({good_ratio})) / {budget}"


def build_alert_rules() -> dict[str, Any]:
    """Build the Prometheus rule-file dict (one group per SLO with burn windows)."""
    groups: list[dict[str, Any]] = []
    for slo in default_slos():
        if not slo.burn_windows:
            continue
        rules: list[dict[str, Any]] = []
        for tier in slo.burn_windows:
            long_expr = _error_ratio_expr(slo, tier.long_window)
            short_expr = _error_ratio_expr(slo, tier.short_window)
            expr = f"({long_expr}) > {tier.burn_rate} and ({short_expr}) > {tier.burn_rate}"
            rules.append(
                {
                    "alert": f"KinoraSLO_{slo.name}_{tier.name}_burn",
                    "expr": expr,
                    "for": tier.short_window,
                    "labels": {
                        "severity": tier.severity,
                        "slo": slo.name,
                        "burn_tier": tier.name,
                    },
                    "annotations": {
                        "summary": (
                            f"{slo.name} burning error budget at >{tier.burn_rate}x "
                            f"({tier.long_window}/{tier.short_window})"
                        ),
                        "description": slo.description,
                    },
                }
            )
        groups.append({"name": f"kinora_slo_{slo.name}", "rules": rules})
    return {"groups": groups}


def build_recording_rules() -> dict[str, Any]:
    """Recording rules that pre-compute each SLO's good-ratio SLI (5m)."""
    rules: list[dict[str, Any]] = []
    for slo in default_slos():
        if not slo.sli_query:
            continue
        rules.append(
            {
                "record": f"kinora:slo_good_ratio:{slo.name}",
                "expr": slo.sli_query,
                "labels": {"slo": slo.name},
            }
        )
    return {"groups": [{"name": "kinora_slo_recording", "rules": rules}]}


# --------------------------------------------------------------------------- #
# A tiny dependency-free YAML emitter for the (known, simple) rule shape.
# --------------------------------------------------------------------------- #


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    # Quote anything with YAML-significant characters to stay safe + round-trippable.
    if text == "" or any(ch in text for ch in ":#{}[],&*!|>'\"%@`") or text != text.strip():
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _emit_yaml(node: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.extend(_emit_yaml(value, indent + 1))
            elif isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}: {{}}" if isinstance(value, dict) else f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                inner = _emit_yaml(item, indent + 1)
                if inner:
                    first = inner[0].lstrip()
                    lines.append(f"{pad}- {first}")
                    lines.extend(inner[1:])
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def to_yaml(rule_file: dict[str, Any]) -> str:
    """Serialize a rule-file dict to YAML text (no external dependency)."""
    return "\n".join(_emit_yaml(rule_file)) + "\n"


def rules_yaml() -> str:
    """The combined recording + alerting rules as a single YAML document."""
    recording = build_recording_rules()
    alerting = build_alert_rules()
    combined = {"groups": recording["groups"] + alerting["groups"]}
    return to_yaml(combined)


__all__ = [
    "build_alert_rules",
    "build_recording_rules",
    "rules_yaml",
    "to_yaml",
]
