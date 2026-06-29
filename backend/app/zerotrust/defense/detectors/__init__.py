"""Streaming threat detectors over normalized security events.

Every detector implements the tiny :class:`Detector` protocol: it consumes one
:class:`~app.zerotrust.defense.types.SecurityEvent` at a time and returns zero or
more :class:`~app.zerotrust.defense.types.Alert` objects. Detectors are **pure**
in the sense that they hold only in-memory state keyed by subject/ip and never do
I/O — the :class:`~app.zerotrust.defense.engine.ThreatEngine` owns fan-out,
deduplication and the store seam.

Concrete detectors:

* :class:`~app.zerotrust.defense.detectors.rate.RateAnomalyDetector` — per-key
  windowed rate against an adaptive baseline (EWMA/MAD).
* :class:`~app.zerotrust.defense.detectors.sequence.SequenceAnomalyDetector` —
  Markov surprise over a subject's action sequence.
* :class:`~app.zerotrust.defense.detectors.behavioral.BehavioralDetector` —
  isolation-forest-lite over a subject's feature vector.
* :class:`~app.zerotrust.defense.detectors.credential_stuffing.CredentialStuffingDetector`
* :class:`~app.zerotrust.defense.detectors.takeover.AccountTakeoverDetector`
* :class:`~app.zerotrust.defense.detectors.scraping.ScrapingDetector`
"""

from __future__ import annotations

from .base import Detector, DetectorBase
from .behavioral import BehavioralConfig, BehavioralDetector
from .credential_stuffing import CredentialStuffingConfig, CredentialStuffingDetector
from .rate import RateAnomalyDetector, RateConfig
from .scraping import ScrapingConfig, ScrapingDetector, ua_suspicion
from .sequence import SequenceAnomalyDetector, SequenceConfig
from .takeover import AccountTakeoverDetector, TakeoverConfig

__all__ = [
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
]
