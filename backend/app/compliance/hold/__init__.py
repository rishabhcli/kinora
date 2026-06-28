"""Legal-hold management: place / lift / scope checks that suspend retention."""

from __future__ import annotations

from app.compliance.hold.service import HoldScope, LegalHoldService

__all__ = ["HoldScope", "LegalHoldService"]
