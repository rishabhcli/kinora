"""Authorization for the MCP tool surface (kinora.md §12).

The streamable-HTTP transport is gated by a single shared ``MCP_AUTH_TOKEN`` +
network isolation. This adds a per-call, book-scoped check on top: a tool call
that names a ``book_id`` is rejected unless that book exists. It closes the
"probe/operate on an arbitrary or forged book_id" gap on the shared-token
surface.

Note on scope: true *per-user* ownership ("does the caller own this book?")
needs a user-scoped identity in the transport, which the single-token HTTP
channel does not carry. That remains the deeper follow-up; this enforces the
existence/validity dimension that is enforceable today. The book-existence
lookup is injected so the policy is unit-testable without a database.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: Resolve whether a book id exists (wired to BookRepo in composition).
BookLookup = Callable[[str], Awaitable[bool]]


class MCPAuthorizationError(Exception):
    """Raised to deny an MCP tool call; the error surfaces to the client."""


class BookScopedAuthorizer:
    """Reject MCP calls that reference an unknown ``book_id`` (§12).

    Implements the :class:`app.mcp.server.ToolAuthorizer` protocol. Calls whose
    arguments carry no ``book_id`` (or a null one) are not book-scoped and pass
    through untouched.
    """

    def __init__(self, *, book_exists: BookLookup) -> None:
        self._book_exists = book_exists

    async def authorize(self, tool_name: str, arguments: dict[str, Any]) -> None:
        book_id = arguments.get("book_id")
        if not book_id:
            return
        if not await self._book_exists(str(book_id)):
            raise MCPAuthorizationError(f"unknown book_id: {book_id}")
