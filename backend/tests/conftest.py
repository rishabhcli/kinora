"""Shared pytest fixtures.

Required settings are populated here *before* the application (and therefore
:class:`app.core.config.Settings`) is imported, so tests never depend on a real
``backend/.env`` or a live DashScope key.
"""

from __future__ import annotations

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "test")
os.environ.setdefault("APP_ENV", "local")

from collections.abc import AsyncIterator

import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Yield an HTTP client bound to a fresh app instance with lifespan run."""
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
