"""Unit tests for the MCP book-scoped authorizer (no infra).

The authorizer guards the streamable-HTTP MCP surface: a call that references a
``book_id`` is rejected unless the book exists. The book-existence lookup is
injected, so these tests need no database.
"""

from __future__ import annotations

import pytest

from app.mcp.authz import BookScopedAuthorizer, MCPAuthorizationError


def _authorizer(*, exists: bool) -> BookScopedAuthorizer:
    async def lookup(_book_id: str) -> bool:
        return exists

    return BookScopedAuthorizer(book_exists=lookup)


async def test_allows_known_book() -> None:
    await _authorizer(exists=True).authorize("canon.query", {"book_id": "book_1"})


async def test_denies_unknown_book() -> None:
    with pytest.raises(MCPAuthorizationError):
        await _authorizer(exists=False).authorize("canon.query", {"book_id": "ghost"})


async def test_allows_call_without_book_id() -> None:
    # A tool that isn't book-scoped (no book_id key) is not the authorizer's concern.
    await _authorizer(exists=False).authorize("prefs.get", {"user_id": "u1"})


async def test_allows_null_book_id() -> None:
    # Optional book_id left null (e.g. global prefs) — nothing to check.
    await _authorizer(exists=False).authorize("prefs.get", {"book_id": None, "user_id": "u1"})
