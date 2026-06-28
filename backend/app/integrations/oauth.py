"""OAuth2 authorization-code flow + token refresh, over the injected HTTP seam.

This is provider-agnostic: a :class:`OAuth2Config` describes one provider's
endpoints + client credentials, and :class:`OAuth2Client` performs the three
flows the app-connection store needs:

* :meth:`authorize_url` — build the URL the user is sent to (with a CSRF
  ``state``);
* :meth:`exchange_code` — swap the returned ``code`` for a :class:`TokenSet`;
* :meth:`refresh` — swap a refresh token for a fresh :class:`TokenSet`.

It never opens a socket itself — every request goes through
:class:`~app.integrations.http.AsyncHttpClient`, so tests drive the whole flow
with :class:`~app.integrations.http.FakeHttpClient` and zero network.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from app.integrations.clock import Clock, SystemClock
from app.integrations.errors import AuthExpired, ConfigurationError
from app.integrations.http import AsyncHttpClient


@dataclass(frozen=True)
class OAuth2Config:
    """One provider's OAuth2 endpoints + this app's client credentials."""

    provider: str
    client_id: str
    client_secret: str
    authorize_endpoint: str
    token_endpoint: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()
    #: Some providers (Pocket) use a non-standard flow; left as a hint flag.
    extra_authorize_params: dict[str, str] = field(default_factory=dict)

    def require(self) -> OAuth2Config:
        """Validate the config is complete, else raise :class:`ConfigurationError`."""
        missing = [
            n for n, v in (
                ("client_id", self.client_id),
                ("client_secret", self.client_secret),
                ("authorize_endpoint", self.authorize_endpoint),
                ("token_endpoint", self.token_endpoint),
                ("redirect_uri", self.redirect_uri),
            ) if not v
        ]
        if missing:
            raise ConfigurationError(
                f"OAuth2 provider {self.provider!r} missing config: {', '.join(missing)}"
            )
        return self


@dataclass(frozen=True)
class TokenSet:
    """The tokens + metadata from a token endpoint response."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    scope: str | None = None
    expires_at: datetime | None = None
    #: Provider extras worth keeping (e.g. Notion ``workspace_name``, account id).
    extra: dict[str, str] = field(default_factory=dict)

    def is_expired(self, *, now: datetime, skew_s: float = 60.0) -> bool:
        """Whether the access token is expired (with a refresh-early skew)."""
        if self.expires_at is None:
            return False
        return now >= (self.expires_at - timedelta(seconds=skew_s))

    def as_blob(self) -> dict[str, object]:
        """Serialise to a JSON-able mapping for the token sealer."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_blob(cls, blob: dict[str, object]) -> TokenSet:
        """Reconstruct from a sealed blob (the inverse of :meth:`as_blob`)."""
        expires_raw = blob.get("expires_at")
        expires_at = (
            datetime.fromisoformat(expires_raw) if isinstance(expires_raw, str) else None
        )
        extra_raw = blob.get("extra")
        extra = (
            {str(k): str(v) for k, v in extra_raw.items()}
            if isinstance(extra_raw, dict)
            else {}
        )
        return cls(
            access_token=str(blob.get("access_token", "")),
            refresh_token=(str(blob["refresh_token"]) if blob.get("refresh_token") else None),
            token_type=str(blob.get("token_type", "Bearer")),
            scope=(str(blob["scope"]) if blob.get("scope") else None),
            expires_at=expires_at,
            extra=extra,
        )


class OAuth2Client:
    """Performs the OAuth2 authorization-code flow over the injected HTTP seam."""

    def __init__(
        self,
        config: OAuth2Config,
        http: AsyncHttpClient,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self._clock = clock or SystemClock()

    @property
    def config(self) -> OAuth2Config:
        """The provider config this client is bound to."""
        return self._config

    def authorize_url(self, *, state: str | None = None) -> tuple[str, str]:
        """Build the authorization URL the user visits + the CSRF ``state``.

        Returns ``(url, state)``; persist ``state`` and verify it on callback.
        """
        self._config.require()
        state = state or secrets.token_urlsafe(24)
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self._config.scopes:
            params["scope"] = " ".join(self._config.scopes)
        params.update(self._config.extra_authorize_params)
        return f"{self._config.authorize_endpoint}?{urlencode(params)}", state

    async def exchange_code(self, code: str) -> TokenSet:
        """Exchange an authorization ``code`` for a :class:`TokenSet`."""
        self._config.require()
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._config.redirect_uri,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        """Exchange a refresh token for a fresh :class:`TokenSet`."""
        self._config.require()
        if not refresh_token:
            raise AuthExpired("no refresh token available")
        new = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            }
        )
        # Many providers omit the refresh_token on refresh; keep the old one.
        if new.refresh_token is None:
            return TokenSet(
                access_token=new.access_token,
                refresh_token=refresh_token,
                token_type=new.token_type,
                scope=new.scope,
                expires_at=new.expires_at,
                extra=new.extra,
            )
        return new

    async def _token_request(self, form: dict[str, str]) -> TokenSet:
        resp = await self._http.request(
            "POST",
            self._config.token_endpoint,
            data=form,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise AuthExpired("token endpoint returned no access_token")
        return self._parse_token_payload(payload)

    def _parse_token_payload(self, payload: dict[str, object]) -> TokenSet:
        expires_at: datetime | None = None
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = self._clock.now() + timedelta(seconds=float(expires_in))
        known = {"access_token", "refresh_token", "token_type", "scope", "expires_in"}
        extra = {str(k): str(v) for k, v in payload.items() if k not in known and v is not None}
        refresh = payload.get("refresh_token")
        return TokenSet(
            access_token=str(payload["access_token"]),
            refresh_token=(str(refresh) if refresh else None),
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=(str(payload["scope"]) if payload.get("scope") else None),
            expires_at=expires_at,
            extra=extra,
        )

    def _now(self) -> datetime:
        return self._clock.now() if self._clock else datetime.now(UTC)


__all__ = ["OAuth2Client", "OAuth2Config", "TokenSet"]
