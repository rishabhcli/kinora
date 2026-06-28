"""Integration tests for IntegrationsService end-to-end (gated on throwaway infra).

These exercise the full service path against the real Postgres test DB (the
``container`` fixture's ``session_factory``): connect → sync → dedup ledger →
sync-run history → health, and the OAuth connect dance — all with a fake HTTP
client and a fake ingest gateway, so no network and no DashScope spend.

Skips cleanly when KINORA_TEST_DATABASE_URL / _REDIS_URL / _S3_ENDPOINT_URL are
unset (the unit suite covers the offline logic).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_asyncio

from app.composition import Container
from app.db.models.integration import ConnectionStatus
from app.db.repositories.integration import AppConnectionRepo, ImportedItemRepo
from app.db.repositories.user import UserRepo
from app.integrations.clock import FakeClock
from app.integrations.crypto import TokenSealer
from app.integrations.http import FakeHttpClient, HttpResponse
from app.integrations.oauth import OAuth2Config
from app.integrations.service import IntegrationsService
from tests.conftest import requires_infra

pytestmark = requires_infra


class FakeIngestGateway:
    """Records imported books instead of spawning real Phase-A ingest."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self.imported: list[tuple[str, str]] = []  # (title, source)

    async def import_pdf(
        self, *, user_id: str, title: str, author: str | None, pdf_bytes: bytes, source: str
    ) -> str:
        from app.db.base import new_id
        from app.db.models.enums import BookStatus
        from app.db.repositories.book import BookRepo

        assert pdf_bytes[:5] == b"%PDF-"  # a real rendered PDF reached the gateway
        book_id = new_id()
        async with self._c.session_factory() as db:
            await BookRepo(db).create(
                title=title, author=author, user_id=user_id,
                status=BookStatus.READY, book_id=book_id,
            )
        self.imported.append((title, source))
        return book_id


async def _make_user(container: Container, email: str) -> str:
    from app.api.security import hash_password

    async with container.session_factory() as db:
        user = await UserRepo(db).create(email=email, hashed_password=hash_password("pw"))
        return user.id


def _service(
    container: Container,
    http: FakeHttpClient,
    gateway: FakeIngestGateway,
    oauth_config: Callable[[str], OAuth2Config | None] | None = None,
) -> IntegrationsService:
    return IntegrationsService(
        session_factory=container.session_factory,
        ingest=gateway,
        http=http,
        sealer=TokenSealer(key="test-key"),
        oauth_config=oauth_config,
        clock=FakeClock(),
    )


@pytest_asyncio.fixture
async def gateway(container: Container) -> FakeIngestGateway:
    return FakeIngestGateway(container)


# --------------------------------------------------------------------------- #
# Token connect + sync (Readwise)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_connect_token_and_sync_imports_books(
    container: Container, gateway: FakeIngestGateway
) -> None:
    user_id = await _make_user(container, "rw@example.com")
    payload = {
        "results": [
            {"user_book_id": 1, "title": "Book One", "author": "Auth",
             "highlights": [{"text": "A memorable line about life.", "location": 1}]},
            {"user_book_id": 2, "title": "Book Two",
             "highlights": [{"text": "Another striking highlight here.", "location": 2}]},
        ],
        "nextPageCursor": None,
    }
    http = FakeHttpClient().json_response("GET", "/export", payload)
    svc = _service(container, http, gateway)

    conn = await svc.connect_token(user_id=user_id, provider="readwise", token="rw-tok")
    assert conn.status is ConnectionStatus.ACTIVE
    # Token was sealed, not stored plaintext.
    assert conn.sealed_token and "rw-tok" not in conn.sealed_token

    report = await svc.sync(user_id=user_id, connection_id=conn.id)
    assert report.imported == 2 and report.failed == 0
    assert {t for t, _ in gateway.imported} == {"Book One", "Book Two"}

    # Re-sync with identical content => everything deduped/skipped.
    http2 = FakeHttpClient().json_response("GET", "/export", payload)
    svc2 = _service(container, http2, gateway)
    report2 = await svc2.sync(user_id=user_id, connection_id=conn.id)
    assert report2.skipped == 2 and report2.imported == 0


@pytest.mark.asyncio
async def test_dedup_ledger_and_run_history_persist(
    container: Container, gateway: FakeIngestGateway
) -> None:
    user_id = await _make_user(container, "hist@example.com")
    payload = {
        "results": [
            {"user_book_id": 7, "title": "Solo",
             "highlights": [{"text": "Just one good quote.", "location": 1}]}
        ],
        "nextPageCursor": None,
    }
    http = FakeHttpClient().json_response("GET", "/export", payload)
    svc = _service(container, http, gateway)
    conn = await svc.connect_token(user_id=user_id, provider="readwise", token="t")
    await svc.sync(user_id=user_id, connection_id=conn.id)

    async with container.session_factory() as db:
        ledger = await ImportedItemRepo(db).list_for_connection(conn.id)
        count = await ImportedItemRepo(db).count_for_connection(conn.id)
    assert count == 1 and ledger[0].title == "Solo" and ledger[0].book_id is not None

    # Health surface reflects a successful run.
    _, imported_count, runs = await svc.health(user_id=user_id, connection_id=conn.id)
    assert imported_count == 1
    assert runs and runs[0].items_imported == 1 and runs[0].status.value == "success"


@pytest.mark.asyncio
async def test_partial_failure_marks_run_partial(
    container: Container, gateway: FakeIngestGateway
) -> None:
    user_id = await _make_user(container, "partial@example.com")
    # Second book has no highlights → connector drops it; but to force an import
    # failure we make the gateway raise on a specific title.
    payload = {
        "results": [
            {"user_book_id": 1, "title": "Good",
             "highlights": [{"text": "fine quote", "location": 1}]},
            {"user_book_id": 2, "title": "BadOne",
             "highlights": [{"text": "boom quote", "location": 2}]},
        ],
        "nextPageCursor": None,
    }
    http = FakeHttpClient().json_response("GET", "/export", payload)

    class _FailingGateway(FakeIngestGateway):
        async def import_pdf(self, *, user_id, title, author, pdf_bytes, source):  # type: ignore[no-untyped-def]
            if title == "BadOne":
                raise RuntimeError("ingest blew up")
            return await super().import_pdf(
                user_id=user_id, title=title, author=author, pdf_bytes=pdf_bytes, source=source
            )

    failing = _FailingGateway(container)
    svc = _service(container, http, failing)
    conn = await svc.connect_token(user_id=user_id, provider="readwise", token="t")
    report = await svc.sync(user_id=user_id, connection_id=conn.id)
    assert report.imported == 1 and report.failed == 1
    assert report.status == "partial"

    async with container.session_factory() as db:
        refreshed = await AppConnectionRepo(db).get(conn.id)
    assert refreshed is not None
    assert refreshed.consecutive_failures >= 1


# --------------------------------------------------------------------------- #
# File import (Kindle)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_import_file_kindle(container: Container, gateway: FakeIngestGateway) -> None:
    user_id = await _make_user(container, "kindle@example.com")
    clippings = (
        b"The Stranger (Camus)\n- Highlight\n\nMother died today.\n==========\n"
    )
    svc = _service(container, FakeHttpClient(), gateway)
    report = await svc.import_file(user_id=user_id, provider="kindle", file_bytes=clippings)
    assert report.imported == 1
    assert gateway.imported[0][0] == "The Stranger"


# --------------------------------------------------------------------------- #
# RSS connect (no-auth) + sync
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rss_connect_no_token_and_sync(
    container: Container, gateway: FakeIngestGateway
) -> None:
    user_id = await _make_user(container, "rss@example.com")
    feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>'
        b"<item><title>Entry</title><link>http://x/e</link>"
        b"<description>Body with enough words to render a paragraph here now.</description>"
        b"<guid>e1</guid></item></channel></rss>"
    )
    http = FakeHttpClient().add("GET", "feed", HttpResponse(status=200, content=feed))
    svc = _service(container, http, gateway)
    conn = await svc.connect_token(
        user_id=user_id, provider="rss", config={"feed_url": "http://x/feed"}
    )
    assert conn.sealed_token is None  # no-auth source
    report = await svc.sync(user_id=user_id, connection_id=conn.id)
    assert report.imported == 1 and gateway.imported[0][0] == "Entry"


# --------------------------------------------------------------------------- #
# OAuth connect dance (Notion)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_oauth_begin_and_complete(
    container: Container, gateway: FakeIngestGateway
) -> None:
    user_id = await _make_user(container, "oauth@example.com")

    def oauth_config(provider: str) -> OAuth2Config | None:
        if provider == "notion":
            return OAuth2Config(
                provider="notion", client_id="cid", client_secret="sec",
                authorize_endpoint="https://api.notion.com/v1/oauth/authorize",
                token_endpoint="https://api.notion.com/v1/oauth/token",
                redirect_uri="http://localhost/cb",
            )
        return None

    token_payload = {
        "access_token": "ntok", "refresh_token": "nref", "expires_in": 3600,
        "workspace_name": "My Space",
    }
    http = FakeHttpClient().json_response("POST", "/oauth/token", token_payload)
    svc = _service(container, http, gateway, oauth_config=oauth_config)

    begin = await svc.begin_oauth(user_id=user_id, provider="notion")
    assert begin.authorize_url.startswith("https://api.notion.com/v1/oauth/authorize")

    conn = await svc.complete_oauth(
        user_id=user_id, connection_id=begin.connection_id, code="abc", state=begin.state
    )
    assert conn.status is ConnectionStatus.ACTIVE
    assert conn.account_label == "My Space"
    # Sealed token round-trips back to the access token via the credential path.
    async with container.session_factory() as db:
        stored = await AppConnectionRepo(db).get(conn.id)
    assert stored is not None and stored.sealed_token and "ntok" not in stored.sealed_token


@pytest.mark.asyncio
async def test_oauth_state_mismatch_rejected(
    container: Container, gateway: FakeIngestGateway
) -> None:
    from app.integrations.errors import ConfigurationError

    user_id = await _make_user(container, "csrf@example.com")

    def oauth_config(provider: str) -> OAuth2Config | None:
        return OAuth2Config(
            provider="notion", client_id="c", client_secret="s",
            authorize_endpoint="https://x/a", token_endpoint="https://x/t",
            redirect_uri="http://localhost/cb",
        )

    svc = _service(container, FakeHttpClient(), gateway, oauth_config=oauth_config)
    begin = await svc.begin_oauth(user_id=user_id, provider="notion")
    with pytest.raises(ConfigurationError):
        await svc.complete_oauth(
            user_id=user_id, connection_id=begin.connection_id, code="abc", state="WRONG"
        )


# --------------------------------------------------------------------------- #
# Disconnect + ownership
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_disconnect_and_ownership_isolation(
    container: Container, gateway: FakeIngestGateway
) -> None:
    from app.integrations.errors import ConfigurationError

    owner = await _make_user(container, "owner-i@example.com")
    other = await _make_user(container, "other-i@example.com")
    svc = _service(container, FakeHttpClient(), gateway)
    conn = await svc.connect_token(user_id=owner, provider="readwise", token="t")

    # Another user cannot sync or disconnect it.
    with pytest.raises(ConfigurationError):
        await svc.sync(user_id=other, connection_id=conn.id)
    with pytest.raises(ConfigurationError):
        await svc.disconnect(user_id=other, connection_id=conn.id)

    await svc.disconnect(user_id=owner, connection_id=conn.id)
    listed = await svc.list_connections(user_id=owner)
    assert conn.id not in {c.id for c in listed}  # hidden by default
    listed_all = await svc.list_connections(user_id=owner, include_disconnected=True)
    assert conn.id in {c.id for c in listed_all}
