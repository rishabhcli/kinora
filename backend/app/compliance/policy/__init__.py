"""Policy-as-code: declarative compliance rules + evaluation + reporting."""

from __future__ import annotations

from app.compliance.policy.engine import PolicyEngine, RuleResult
from app.compliance.policy.report import ComplianceReport, build_report
from app.compliance.policy.rules import (
    DEFAULT_RULES,
    ComplianceFacts,
    PolicyRule,
    RuleOutcome,
)

__all__ = [
    "DEFAULT_RULES",
    "ComplianceFacts",
    "ComplianceReport",
    "PolicyEngine",
    "PolicyRule",
    "RuleOutcome",
    "RuleResult",
    "build_report",
]
