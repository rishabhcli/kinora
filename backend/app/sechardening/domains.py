"""Download-domain allow-list: safe external media download gating.

The Kinora render pipeline occasionally needs to fetch externally-hosted media
(e.g. provider-generated video/image URLs that expire).  To prevent server-side
request forgery (SSRF) and accidental data exfiltration, every outbound download
URL must be validated against a configured allow-list of trusted domain suffixes
before the HTTP client fires.

Design decisions
----------------
* **Scheme must be ``https``** — plain HTTP is rejected unconditionally because
  it offers no transport security guarantees and is not acceptable for media
  assets that may contain user data.
* **Host matching uses suffix semantics** — an entry of ``dashscope.com`` allows
  ``dashscope.com`` and ``*.dashscope.com`` (e.g. ``cdn.dashscope.com``).  This
  mirrors how ``Content-Security-Policy`` host-source matching works.
* **No wildcards in config** — the allow-list entries are plain domain names;
  the suffix expansion is done by the matcher, not the config, to avoid
  misconfigurations like ``*.com``.
* **Port stripping** — the ``netloc`` component may include a port; only the
  hostname part is matched so ``cdn.dashscope.com:443`` still matches the
  ``dashscope.com`` entry.
* **Pure string parsing** — uses only :mod:`urllib.parse`; no DNS lookups, no
  network, no I/O.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = [
    "DomainNotAllowedError",
    "is_download_allowed",
    "assert_download_allowed",
    "DEFAULT_ALLOWED_DOMAINS",
]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Conservative default allow-list: the two DashScope CDN / API domains used
#: by the standard render pipeline.  Operators SHOULD override this via the
#: ``SECHARDENING_ALLOWED_DOWNLOAD_DOMAINS`` setting.
DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = (
    "dashscope.aliyuncs.com",
    "dashscope-intl.aliyuncs.com",
    "minimax.io",
    "api.minimax.io",
)


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class DomainNotAllowedError(ValueError):
    """Raised when a URL's host (or scheme) is not on the allow-list.

    Attributes:
        url: The original URL string.
        host: The parsed host, or ``""`` if parsing failed.
        reason: Short machine-readable label (``"scheme"``, ``"host"``,
            ``"empty_host"``, ``"parse_error"``).
    """

    def __init__(self, url: str, host: str, reason: str) -> None:
        self.url = url
        self.host = host
        self.reason = reason
        super().__init__(
            f"Download not allowed [{reason}]: url={url!r}, host={host!r}"
        )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _extract_host(url: str) -> str:
    """Return the bare hostname from *url* (no port, lowercase).

    Raises :exc:`ValueError` if the URL cannot be parsed.
    """
    parsed = urlparse(url)
    # ``hostname`` strips port and lowercases; may be None for opaque URIs.
    host = parsed.hostname or ""
    return host


def _host_matches_entry(host: str, entry: str) -> bool:
    """Return True when *host* matches the allow-list *entry*.

    Matching rules:
    * Exact match: ``host == entry``
    * Subdomain match: ``host`` ends with ``"." + entry``

    Both operands are assumed to already be lowercase.
    """
    entry = entry.lower().strip()
    host = host.lower().strip()
    if not entry:
        return False
    return host == entry or host.endswith("." + entry)


def is_download_allowed(
    url: str,
    allowed_domains: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_DOMAINS,
) -> bool:
    """Return ``True`` if *url* is permitted for outbound download.

    A URL is permitted when:
    1. Its scheme is exactly ``"https"`` (case-insensitive).
    2. Its hostname is non-empty.
    3. Its hostname matches at least one entry in *allowed_domains* via the
       exact-or-subdomain rule described in :func:`_host_matches_entry`.

    Args:
        url: The candidate download URL.
        allowed_domains: Sequence of trusted domain suffixes.  Defaults to
            :data:`DEFAULT_ALLOWED_DOMAINS`.

    Returns:
        ``True`` if allowed, ``False`` otherwise (never raises).
    """
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname or ""
    except Exception:  # noqa: BLE001
        return False

    if scheme != "https":
        return False
    if not host:
        return False
    return any(_host_matches_entry(host, entry) for entry in allowed_domains)


def assert_download_allowed(
    url: str,
    allowed_domains: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_DOMAINS,
) -> None:
    """Raise :exc:`DomainNotAllowedError` if *url* is not permitted.

    Suitable as a pre-condition guard at the start of any function that
    initiates an outbound HTTP download.

    Args:
        url: The candidate download URL.
        allowed_domains: Same semantics as :func:`is_download_allowed`.

    Raises:
        DomainNotAllowedError: With ``reason="scheme"`` for a non-HTTPS URL,
            ``reason="empty_host"`` when the host cannot be determined, or
            ``reason="host"`` when the host is present but not on the list.
    """
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname or ""
    except Exception as exc:  # noqa: BLE001
        raise DomainNotAllowedError(url, "", "parse_error") from exc

    if scheme != "https":
        raise DomainNotAllowedError(url, host, "scheme")
    if not host:
        raise DomainNotAllowedError(url, "", "empty_host")
    if not any(_host_matches_entry(host, entry) for entry in allowed_domains):
        raise DomainNotAllowedError(url, host, "host")
