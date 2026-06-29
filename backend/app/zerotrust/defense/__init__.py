"""Zero-trust defense: runtime threat detection + build-time supply-chain.

This facet of the zero-trust subsystem owns two cooperating layers, both built as
**pure libraries** (no FastAPI / DB / network imports at module scope) over a
deterministic clock and a small store seam, so they replay byte-stably over
synthetic attack traces.

Runtime defense
---------------
* :class:`~app.zerotrust.defense.engine.ThreatEngine` — streams normalized
  security events to a fan-out of detectors, deduplicates the alerts, and writes
  them to an :class:`~app.zerotrust.defense.store.AlertSink`.
* Detectors: rate / sequence / behavioural anomaly, credential-stuffing, account
  takeover, scraping (``app.zerotrust.defense.detectors``).
* :class:`~app.zerotrust.defense.waf.WAFEngine` — a request-policy engine with
  signature rules + a bot score.

Build-time / supply-chain defense
----------------------------------
* :func:`~app.zerotrust.defense.supplychain.sbom.generate_sbom` — parse the repo
  lockfiles into a CycloneDX-shaped SBOM.
* :class:`~app.zerotrust.defense.supplychain.vuln.VulnScanner` — match SBOM
  components against an injectable advisory database.
* :mod:`~app.zerotrust.defense.supplychain.provenance` — artifact signing +
  SLSA-shaped provenance verification.

Import side effects: none. Construct a :class:`ThreatEngine` and a
:class:`WAFEngine` explicitly; nothing here reads settings on import.
"""

from __future__ import annotations

from .alerting import DedupConfig, Deduper
from .clock import Clock, ManualClock, SystemClock
from .detectors.base import Detector, DetectorBase
from .detectors.behavioral import BehavioralConfig, BehavioralDetector
from .detectors.credential_stuffing import (
    CredentialStuffingConfig,
    CredentialStuffingDetector,
)
from .detectors.rate import RateAnomalyDetector, RateConfig
from .detectors.scraping import ScrapingConfig, ScrapingDetector, ua_suspicion
from .detectors.sequence import SequenceAnomalyDetector, SequenceConfig
from .detectors.takeover import AccountTakeoverDetector, TakeoverConfig
from .engine import EngineStats, ThreatEngine
from .errors import (
    ConfigError,
    DefenseError,
    LockfileParseError,
    ProvenanceError,
    RuleCompileError,
)
from .stats import Ewma, IsolationForestLite, Mad, RobustScaler
from .store import AlertSink, InMemoryAlertStore, NullAlertSink
from .types import (
    Alert,
    AuthOutcome,
    EventKind,
    SecurityEvent,
    Severity,
    ThreatCategory,
)
from .windows import DistinctWindow, SlidingCounter

__all__ = [
    # types
    "Alert",
    "AuthOutcome",
    "EventKind",
    "SecurityEvent",
    "Severity",
    "ThreatCategory",
    # clock
    "Clock",
    "ManualClock",
    "SystemClock",
    # stats / windows
    "DistinctWindow",
    "Ewma",
    "IsolationForestLite",
    "Mad",
    "RobustScaler",
    "SlidingCounter",
    # engine / alerting / store
    "AlertSink",
    "DedupConfig",
    "Deduper",
    "EngineStats",
    "InMemoryAlertStore",
    "NullAlertSink",
    "ThreatEngine",
    # detectors
    "AccountTakeoverDetector",
    "BehavioralConfig",
    "BehavioralDetector",
    "CredentialStuffingConfig",
    "CredentialStuffingDetector",
    "Detector",
    "DetectorBase",
    "RateAnomalyDetector",
    "RateConfig",
    "ScrapingConfig",
    "ScrapingDetector",
    "SequenceAnomalyDetector",
    "SequenceConfig",
    "TakeoverConfig",
    "ua_suspicion",
    # errors
    "ConfigError",
    "DefenseError",
    "LockfileParseError",
    "ProvenanceError",
    "RuleCompileError",
]
