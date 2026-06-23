"""Directing-style preference routes — "Your directing style" (kinora.md §8.6).

The cross-session preference loop (a Director note → a learned prior → a shifted
default on the next session) is otherwise invisible. These routes surface it:

* ``GET /me/prefs`` — the reader's directing style across **all** their books.
* ``GET /books/{id}/prefs`` — the style learned for **one** book (the same
  book-scoped priors the Cinematographer reads when designing that book's shots).
* ``DELETE /me/prefs`` / ``DELETE /books/{id}/prefs`` — reset, globally or per
  book.

Aggregated priors are projected into plain language by
:mod:`app.memory.prefs_signals` ("You prefer slower shots", "Warmer palette bias
+0.3") so the panel reads like a sentence, not a vector.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.api.schemas import (
    DirectingPriorView,
    DirectingStyleResponse,
    ResetPrefsResponse,
)
from app.core.logging import get_logger
from app.db.repositories.book import BookRepo
from app.memory.prefs_service import PreferencePrior, PreferencePriors
from app.memory.prefs_signals import (
    AXIS_KINDS,
    applied_value,
    bias_of,
    describe,
    is_applied,
)

logger = get_logger("app.api.prefs")

router = APIRouter(tags=["prefs"])


def prior_view(prior: PreferencePrior) -> DirectingPriorView:
    """Project one aggregated prior into its plain-language panel row (§8.6)."""
    label, detail = describe(prior)
    note = prior.value.get("note") if isinstance(prior.value, dict) else None
    return DirectingPriorView(
        kind=prior.kind,
        bias=bias_of(prior),
        weight=prior.weight,
        label=label,
        detail=detail,
        applied=is_applied(prior),
        applied_value=applied_value(prior),
        last_note=note if isinstance(note, str) else None,
    )


def _views(priors: PreferencePriors) -> list[DirectingPriorView]:
    """Project aggregated priors into plain-language panel rows (known axes first)."""
    ordered = [*AXIS_KINDS, *(k for k in priors.priors if k not in AXIS_KINDS)]
    return [prior_view(p) for kind in ordered if (p := priors.priors.get(kind)) is not None]


async def _assert_book_owner(container: ContainerDep, user: CurrentUser, book_id: str) -> None:
    """Fail-closed ownership check via durable ``books.user_id`` (mirrors §5.4)."""
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)


@router.get("/me/prefs", response_model=DirectingStyleResponse)
async def my_directing_style(container: ContainerDep, user: CurrentUser) -> DirectingStyleResponse:
    """The reader's accumulated directing style across all their books (§8.6)."""
    priors = await container.get_prefs(user_id=user.id)
    return DirectingStyleResponse(scope="user", priors=_views(priors))


@router.get("/books/{book_id}/prefs", response_model=DirectingStyleResponse)
async def book_directing_style(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> DirectingStyleResponse:
    """The directing style learned for one book — what its shots default to (§8.6)."""
    await _assert_book_owner(container, user, book_id)
    priors = await container.get_prefs(book_id=book_id)
    return DirectingStyleResponse(scope="book", book_id=book_id, priors=_views(priors))


@router.delete("/me/prefs", response_model=ResetPrefsResponse)
async def reset_my_directing_style(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ResetPrefsResponse:
    """Clear the reader's learned directing style everywhere (global reset, §8.6)."""
    cleared = await container.reset_prefs(user_id=user.id)
    logger.info("prefs.reset", scope="user", user_id=user.id, cleared=cleared)
    return ResetPrefsResponse(scope="user", cleared=cleared)


@router.delete("/books/{book_id}/prefs", response_model=ResetPrefsResponse)
async def reset_book_directing_style(
    book_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ResetPrefsResponse:
    """Clear the directing style learned for one book (§8.6)."""
    await _assert_book_owner(container, user, book_id)
    cleared = await container.reset_prefs(book_id=book_id)
    logger.info("prefs.reset", scope="book", book_id=book_id, cleared=cleared)
    return ResetPrefsResponse(scope="book", book_id=book_id, cleared=cleared)


__all__ = ["router"]
