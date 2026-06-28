"""Compliance & consent subsystem (GDPR/CCPA governance).

This package owns the *governance* layer around Kinora's personal data:

* **consent** — versioned policies + purpose-based grant/withdraw + proof records;
* **retention** — per-data-class TTL + lawful-basis tagging + expiry candidates;
* **hold** — legal holds that suspend retention/erasure;
* **dsar** — the data-subject-access-request workflow state machine;
* **ledger** — a consolidated, hash-chained, tamper-evident compliance audit log;
* **policy** — policy-as-code rules + evaluation + a consolidated report.

It **complements** the ``dataportability`` domain (which executes GDPR export and
erasure): this package decides *whether* and *for how long* data may be kept and
*records* the legal basis, while delegating the actual export/erasure mechanics
to an injected :class:`~app.compliance.dsar.service.Fulfiller` seam.

See ``DESIGN.md`` in this package and kinora.md §8 / §11 for the design context.
"""

from __future__ import annotations
