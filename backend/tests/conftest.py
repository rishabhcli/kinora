"""Shared pytest fixtures + cross-test state isolation.

Required settings are populated here *before* the application (and therefore
:class:`app.core.config.Settings`) is imported, so tests never depend on a real
``backend/.env`` or a live DashScope key.

**Isolation (the fix for cross-test bleed).** Several integration tests commit to
the shared throwaway Postgres (e.g. the MCP tools reserve budget through a
*committing* unit of work), and the budget ledger / canon are process-global. The
autouse :func:`_isolate_state` fixture gives every test a CLEAN slate: when a test
database is configured it ensures the schema then ``TRUNCATE``\\ s every table
before the test runs, and when a test Redis is configured it ``FLUSHDB``\\ s it.
With this in place ``test_memory_budget`` and ``test_mcp_tools`` pass together.

When no infra is configured the fixture is a no-op and the infra-bound API
fixtures skip cleanly, so the unit suite still runs anywhere.
"""

from __future__ import annotations

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "test")
os.environ.setdefault("APP_ENV", "local")

import hashlib
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.composition import CommentRoute, Container, RegenOutcome, build_container
from app.core.config import Settings
from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base, new_id
from app.db.models.enums import BookStatus
from app.db.repositories.book import BookRepo
from app.main import create_app

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")
_S3_ENDPOINT = os.environ.get("KINORA_TEST_S3_ENDPOINT_URL") or os.environ.get(
    "KINORA_TEST_S3_ENDPOINT"
)
_EMBED_DIM = 1152


# --------------------------------------------------------------------------- #
# Cross-test isolation (autouse)
# --------------------------------------------------------------------------- #


async def _truncate_all() -> None:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
            tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            if tables:
                await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


async def _flush_redis() -> None:
    assert _REDIS_URL is not None
    from redis.asyncio import Redis

    client: Redis = Redis.from_url(_REDIS_URL, decode_responses=True)
    try:
        await client.flushdb()
    finally:
        # redis.asyncio exposes aclose() at runtime; the pinned types-redis stub
        # lacks it (app.redis.client types the handle as Any for the same drift).
        await client.aclose()  # type: ignore[attr-defined]


@pytest_asyncio.fixture(autouse=True)
async def _isolate_state() -> AsyncIterator[None]:
    """Give each test a clean DB + Redis (fixes budget/canon bleed across files)."""
    if _DB_URL:
        await _truncate_all()
    if _REDIS_URL:
        await _flush_redis()
    yield


# --------------------------------------------------------------------------- #
# Meta-endpoint client (no infrastructure required)
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client over a fresh app — used by the meta/health tests (no infra)."""
    app = create_app()
    app.state.run_idle_sweeper = False
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


# --------------------------------------------------------------------------- #
# API gateway fixtures (require throwaway Postgres + Redis + MinIO)
# --------------------------------------------------------------------------- #

requires_infra = pytest.mark.skipif(
    not (_DB_URL and _REDIS_URL and _S3_ENDPOINT),
    reason="API gateway tests require KINORA_TEST_DATABASE_URL + _REDIS_URL + _S3_ENDPOINT_URL",
)


def build_test_settings() -> Settings:
    """Settings pointed at the throwaway infra, with small/safe budget caps."""
    assert _DB_URL and _REDIS_URL and _S3_ENDPOINT
    return Settings(
        dashscope_api_key="test",
        app_env="local",
        jwt_secret="kinora-test-jwt-secret-key-which-is-comfortably-32-bytes",
        database_url=_DB_URL,
        redis_url=_REDIS_URL,
        s3_endpoint_url=_S3_ENDPOINT,
        s3_access_key=os.environ.get("KINORA_TEST_S3_ACCESS_KEY", "kinora"),
        s3_secret_key=os.environ.get("KINORA_TEST_S3_SECRET_KEY", "kinora-secret"),
        s3_region=os.environ.get("KINORA_TEST_S3_REGION", "us-east-1"),
        s3_bucket=os.environ.get("KINORA_TEST_S3_BUCKET", "kinora"),
        kinora_live_video=False,
        budget_ceiling_video_s=300.0,
        budget_per_session_s=120.0,
        budget_per_scene_s=60.0,
        budget_low_floor_s=30.0,
    )


class FakeEmbedder:
    """Deterministic one-hot embedder so canon writes never hit the network."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._one_hot(t.encode("utf-8")) for t in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._one_hot(b) for b in images]

    @staticmethod
    def _one_hot(data: bytes) -> list[float]:
        axis = int.from_bytes(hashlib.sha1(data).digest()[:4], "big") % _EMBED_DIM
        vector = [0.0] * _EMBED_DIM
        vector[axis] = 1.0
        return vector


class FakeCommentClassifier:
    """Keyword router mirroring the real fallback — no chat() network in tests."""

    async def classify(self, note: str, *, shot_context: str | None = None) -> CommentRoute:
        text_l = note.lower()
        if any(w in text_l for w in ("room", "location", "place", "wrong", "where")):
            return CommentRoute(agent="continuity", aspect="room", message=note)
        if any(w in text_l for w in ("fast", "slow", "pace", "linger", "speed")):
            return CommentRoute(agent="cinematographer", aspect="pacing", message=note)
        return CommentRoute(agent="cinematographer", aspect="look", message=note)


class FakeIngestRunner:
    """Records that ingest was triggered and marks the book ready (no providers)."""

    def __init__(self, container: Container) -> None:
        self._container = container
        self.calls: list[str] = []

    async def __call__(self, book_id: str, pdf_bytes: bytes, session_id: str | None) -> None:
        self.calls.append(book_id)
        async with self._container.session_factory() as session:
            repo = BookRepo(session)
            await repo.set_num_pages(book_id, 1)
            await repo.set_status(book_id, BookStatus.READY)
        await self._container.redis.set_json(
            f"kinora:book:progress:{book_id}", {"stage": "ready", "pct": 1.0}
        )


async def fake_regen_runner(
    book_id: str, shot_id: str, session_id: str | None
) -> RegenOutcome:
    """A canned, network-free regen outcome for surgical-regen tests."""
    return RegenOutcome(
        shot_id=shot_id,
        status="accepted",
        oss_url=f"https://example.invalid/clips/{shot_id}.mp4",
        qa={"verdict": "pass", "ccs": 0.92},
    )


@pytest_asyncio.fixture
async def container() -> AsyncIterator[Container]:
    """A wired container against the throwaway infra, with network seams faked."""
    if not (_DB_URL and _REDIS_URL and _S3_ENDPOINT):
        pytest.skip("API gateway tests require throwaway Postgres + Redis + MinIO")
    c = build_container(build_test_settings())
    c.embedder = FakeEmbedder()
    c.comment_classifier = FakeCommentClassifier()
    c.ingest_runner = FakeIngestRunner(c)
    c.regen_runner = fake_regen_runner
    yield c
    # The app lifespan owns shutdown; nothing to close here.


@pytest_asyncio.fixture
async def api_client(container: Container) -> AsyncIterator[AsyncClient]:
    """An HTTP client over the gateway with the test container injected."""
    app = create_app()
    app.state.container = container
    app.state.run_idle_sweeper = False
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def register_login(
    client: AsyncClient, email: str, password: str = "password123"
) -> dict[str, str]:
    """Register (idempotently) + log in; return an Authorization header dict."""
    await client.post("/api/auth/register", json={"email": email, "password": password})
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def auth_headers(api_client: AsyncClient) -> dict[str, str]:
    """Authorization header for a default registered user."""
    return await register_login(api_client, "owner@example.com")


@pytest.fixture
def make_user(api_client: AsyncClient) -> Callable[[str], object]:
    """Factory returning a coroutine that registers+logs in a fresh user."""

    async def _make(email: str) -> dict[str, str]:
        return await register_login(api_client, email)

    return _make


async def user_id_for(client: AsyncClient, headers: dict[str, str]) -> str:
    """Resolve the authenticated user's id via ``/auth/me``."""
    resp = await client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


async def seed_owned_book(
    client: AsyncClient,
    container: Container,
    headers: dict[str, str],
    *,
    title: str = "Seeded Tale",
    status: BookStatus = BookStatus.READY,
    art_direction: str | None = None,
) -> str:
    """Create a book row + register the caller as its owner (no upload/ingest)."""
    uid = await user_id_for(client, headers)
    book_id = new_id()
    async with container.session_factory() as session:
        await BookRepo(session).create(
            title=title, book_id=book_id, status=status, art_direction=art_direction
        )
    await container.redis.raw.sadd(f"kinora:user:{uid}:books", book_id)
    return book_id


def tiny_pdf(text_line: str = "Kinora — watch the book.") -> bytes:
    """Build a minimal, real one-page PDF with PyMuPDF (no network, no fixtures)."""
    import fitz  # PyMuPDF

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text_line)
    data: bytes = doc.tobytes()
    doc.close()
    return data
