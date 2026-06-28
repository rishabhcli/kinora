"""FastAPI surface for the compliance subsystem."""

from __future__ import annotations

from app.compliance.api.routes import router

__all__ = ["router"]
