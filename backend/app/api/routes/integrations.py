"""Integrations API — connect sources, sync, import files, see health (§9.1).

This is the HTTP surface over :class:`app.integrations.service.IntegrationsService`.
Every route is owner-scoped to the authenticated reader; imported items become
first-class books that flow through the unchanged Phase-A pipeline.

Routes (all under ``/integrations``):

* ``GET  /providers`` — connectable sources + their capabilities.
* ``GET  /connections`` — the reader's connections + health.
* ``POST /connections`` — connect a token/no-auth source (Readwise / RSS / web).
* ``POST /oauth/start`` — begin the OAuth2 connect dance (returns authorize URL).
* ``POST /oauth/complete`` — finish OAuth2 with the callback code.
* ``POST /import/file`` — one-shot file-upload import (Kindle clippings / OPML).
* ``POST /connections/{id}/sync`` — run an incremental sync now.
* ``GET  /connections/{id}`` — one connection's detailed health + run history.
* ``DELETE /connections/{id}`` — disconnect.
* ``POST /webhooks/{provider}`` — verified push-sync receiver.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger
from app.db.models.integration import AppConnection
from app.integrations.errors import (
    AuthExpired,
    ConfigurationError,
    IntegrationError,
    WebhookVerificationError,
)
from app.integrations.health import ConnectionHealth
from app.integrations.service import IntegrationsService
from app.integrations.sync import SyncReport
from app.integrations.webhooks import WebhookSecret, WebhookVerifier

logger = get_logger("app.api.integrations")

router = APIRouter(prefix="/integrations", tags=["integrations"])

#: Hard cap for an uploaded clippings / OPML file (these are text, never large).
_MAX_IMPORT_FILE_BYTES = 32 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ProviderResponse(BaseModel):
    """A connectable source + how to drive it."""

    name: str
    display_name: str
    capabilities: list[str]
    auth_hint: str


class ConnectRequest(BaseModel):
    """Connect a token-auth source or configure a no-auth one."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    token: str | None = Field(default=None, max_length=4096)
    config: dict[str, Any] = Field(default_factory=dict)
    account_label: str | None = Field(default=None, max_length=512)


class OAuthStartRequest(BaseModel):
    """Begin an OAuth2 connect for a provider."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)


class OAuthStartResponse(BaseModel):
    """The authorize URL + the connection that was opened (PENDING)."""

    connection_id: str
    authorize_url: str
    state: str


class OAuthCompleteRequest(BaseModel):
    """Finish OAuth2 with the provider's callback parameters."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    code: str
    state: str


class SyncReportResponse(BaseModel):
    """The result of a sync / file import."""

    status: str
    seen: int
    imported: int
    skipped: int
    failed: int
    auth_expired: bool
    error: str | None = None

    @classmethod
    def of(cls, report: SyncReport) -> SyncReportResponse:
        return cls(
            status=report.status,
            seen=report.seen,
            imported=report.imported,
            skipped=report.skipped,
            failed=report.failed,
            auth_expired=report.auth_expired,
            error=report.fatal_error,
        )


class ConnectionResponse(BaseModel):
    """A connection's health row for the settings panel."""

    id: str
    provider: str
    account_label: str | None
    status: str
    health: str
    last_synced_at: str | None
    last_error: str | None
    consecutive_failures: int
    imported_count: int
    needs_reauth: bool
    recent_runs: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def of(cls, h: ConnectionHealth) -> ConnectionResponse:
        return cls(
            id=h.id,
            provider=h.provider,
            account_label=h.account_label,
            status=h.status,
            health=h.health,
            last_synced_at=h.last_synced_at,
            last_error=h.last_error,
            consecutive_failures=h.consecutive_failures,
            imported_count=h.imported_count,
            needs_reauth=h.needs_reauth,
            recent_runs=[vars(r) for r in h.recent_runs],
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _service(container: ContainerDep) -> IntegrationsService:
    return container.build_integrations()


def _map_error(exc: IntegrationError) -> APIError:
    """Translate an integrations error into the gateway envelope."""
    if isinstance(exc, AuthExpired):
        return APIError("integration_auth_expired", str(exc), status=401)
    if isinstance(exc, ConfigurationError):
        return APIError("integration_misconfigured", str(exc), status=400)
    return APIError("integration_error", str(exc), status=502)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/providers", response_model=list[ProviderResponse])
async def list_providers(container: ContainerDep, user: CurrentUser) -> list[ProviderResponse]:
    """List the connectable sources + their capabilities."""
    svc = _service(container)
    return [
        ProviderResponse(
            name=info.name,
            display_name=info.display_name,
            capabilities=sorted(c.value for c in info.capabilities),
            auth_hint=info.auth_hint,
        )
        for info in svc.list_providers()
    ]


@router.get("/connections", response_model=list[ConnectionResponse])
async def list_connections(
    container: ContainerDep, user: CurrentUser
) -> list[ConnectionResponse]:
    """List the reader's connections with their current health."""
    svc = _service(container)
    conns = await svc.list_connections(user_id=user.id)
    out: list[ConnectionResponse] = []
    for conn in conns:
        _, count, runs = await svc.health(user_id=user.id, connection_id=conn.id)
        health = ConnectionHealth.of(conn, imported_count=count, recent_runs=runs)
        out.append(ConnectionResponse.of(health))
    return out


@router.post(
    "/connections", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED
)
async def connect(
    body: ConnectRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ConnectionResponse:
    """Connect a token-auth source (Readwise) or configure a no-auth one (RSS/web)."""
    svc = _service(container)
    try:
        conn = await svc.connect_token(
            user_id=user.id,
            provider=body.provider,
            token=body.token,
            config=body.config,
            account_label=body.account_label,
        )
    except IntegrationError as exc:
        raise _map_error(exc) from exc
    return _connection_view(conn)


@router.post("/oauth/start", response_model=OAuthStartResponse)
async def oauth_start(
    body: OAuthStartRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> OAuthStartResponse:
    """Begin an OAuth2 connect; returns the provider authorize URL + CSRF state."""
    svc = _service(container)
    try:
        result = await svc.begin_oauth(user_id=user.id, provider=body.provider)
    except IntegrationError as exc:
        raise _map_error(exc) from exc
    return OAuthStartResponse(
        connection_id=result.connection_id, authorize_url=result.authorize_url, state=result.state
    )


@router.post("/oauth/complete", response_model=ConnectionResponse)
async def oauth_complete(
    body: OAuthCompleteRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ConnectionResponse:
    """Finish an OAuth2 connect by exchanging the callback code for tokens."""
    svc = _service(container)
    try:
        conn = await svc.complete_oauth(
            user_id=user.id, connection_id=body.connection_id, code=body.code, state=body.state
        )
    except IntegrationError as exc:
        raise _map_error(exc) from exc
    return _connection_view(conn)


@router.post("/import/file", response_model=SyncReportResponse)
async def import_file(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    provider: Annotated[str, Form()],
    file: Annotated[UploadFile, File(description="Kindle clippings / OPML file")],
) -> SyncReportResponse:
    """One-shot file-upload import (Kindle ``My Clippings.txt`` / OPML)."""
    data = await file.read()
    if len(data) > _MAX_IMPORT_FILE_BYTES:
        raise APIError("file_too_large", "import file exceeds the size limit", status=413)
    svc = _service(container)
    try:
        report = await svc.import_file(user_id=user.id, provider=provider, file_bytes=data)
    except IntegrationError as exc:
        raise _map_error(exc) from exc
    return SyncReportResponse.of(report)


@router.post("/connections/{connection_id}/sync", response_model=SyncReportResponse)
async def sync_connection(
    connection_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> SyncReportResponse:
    """Run an incremental sync for a connection now."""
    svc = _service(container)
    try:
        report = await svc.sync(user_id=user.id, connection_id=connection_id, trigger="manual")
    except IntegrationError as exc:
        raise _map_error(exc) from exc
    return SyncReportResponse.of(report)


@router.get("/connections/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: str, container: ContainerDep, user: CurrentUser
) -> ConnectionResponse:
    """One connection's detailed health + recent run history."""
    svc = _service(container)
    try:
        conn, count, runs = await svc.health(user_id=user.id, connection_id=connection_id)
    except IntegrationError as exc:
        raise _map_error(exc) from exc
    return ConnectionResponse.of(
        ConnectionHealth.of(conn, imported_count=count, recent_runs=runs)
    )


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    connection_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> None:
    """Disconnect a connection (kept for history; no longer synced)."""
    svc = _service(container)
    try:
        await svc.disconnect(user_id=user.id, connection_id=connection_id)
    except IntegrationError as exc:
        raise _map_error(exc) from exc


@router.post("/webhooks/{provider}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    provider: str, request: Request, container: ContainerDep
) -> dict[str, str]:
    """Verified push-sync receiver.

    The provider posts a signed payload; if it verifies against the configured
    per-provider secret the request is accepted (the actual sync fan-out is left
    to the operator's chosen trigger — this endpoint's job is verification + a
    202). An unverified payload is rejected with 401.
    """
    verifier = _webhook_verifier(container)
    body = await request.body()
    try:
        verifier.verify(provider, body, dict(request.headers))
    except WebhookVerificationError as exc:
        raise APIError("webhook_unverified", str(exc), status=401) from exc
    logger.info("integrations.webhook.accepted", provider=provider, bytes=len(body))
    return {"status": "accepted", "provider": provider}


def _connection_view(conn: AppConnection) -> ConnectionResponse:
    """Project a freshly-mutated connection (no run history yet) for the response."""
    return ConnectionResponse.of(ConnectionHealth.of(conn, imported_count=0, recent_runs=[]))


def _webhook_verifier(container: ContainerDep) -> WebhookVerifier:
    """Build a verifier from the configured per-provider webhook secrets."""
    s = container.settings
    verifier = WebhookVerifier()
    if s.readwise_webhook_secret:
        verifier.register(WebhookSecret(provider="readwise", secret=s.readwise_webhook_secret))
    if s.notion_webhook_secret:
        verifier.register(WebhookSecret(provider="notion", secret=s.notion_webhook_secret))
    return verifier


__all__ = ["router"]
