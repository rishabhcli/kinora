"""The composition root — wire every subsystem into one runnable :class:`Container`.

This is the single place the dependency-injection seams the earlier phases left
open are satisfied:

* the memory layer's :class:`~app.memory.interfaces.RenderEnqueuer` seam (Phase 4
  shipped a ``NotWired`` default) is satisfied by the **real**
  :class:`~app.queue.enqueuer.RedisRenderEnqueuer` over the Redis priority queue;
* the :class:`~app.memory.interfaces.ShotPlanner` seam is satisfied by the **real**
  :class:`~app.agents.adapter.Adapter`.

Both are injected into :class:`~app.mcp.tools.MemoryTools` (so the MCP ``shot.render``
tool enqueues real render jobs and ``shot.plan`` runs the real Adapter), and the
same enqueuer drives Director-mode targeted regen.

Everything heavy is built lazily: constructing a :class:`Container` opens no
sockets and needs no network, so ``create_app()`` and the ``/health`` probe work
with ``DASHSCOPE_API_KEY=test`` and no infrastructure. Providers (DashScope
clients), the render pipeline, and the ingest pipeline are imported and
constructed only on first use.

The provider-calling collaborators are also exposed as overridable *seams*
(:attr:`Container.comment_classifier`, :attr:`Container.regen_runner`,
:attr:`Container.ingest_runner`, :attr:`Container.shot_planner`,
:attr:`Container.embedder`) so tests can drive the gateway end-to-end without the
network while production uses the real defaults.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.enums import BookStatus, RenderPriority
from app.memory.budget_service import BudgetLimits, BudgetService
from app.memory.interfaces import Embedder, RenderEnqueuer, ShotPlanner, ShotSpec
from app.queue.enqueuer import RedisRenderEnqueuer
from app.queue.redis_queue import RedisRenderQueue, book_channel, library_channel, session_channel
from app.redis.client import RedisClient
from app.scheduler.intent import IntentController
from app.scheduler.model import SchedulerStore
from app.scheduler.service import QueueKeyframeMaintainer, SchedulerService
from app.storage.object_store import ObjectStore

if TYPE_CHECKING:
    from app.mcp.authz import BookScopedAuthorizer
    from app.mcp.tools import MemoryTools
    from app.memory.prefs_service import PreferencePrior, PreferencePriors
    from app.providers import Providers
    from app.scheduler.keyframe import KeyframeService

logger = get_logger("app.composition")

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
INGEST_RECOVERY_LIMIT = 25
# A single ingest can run for many minutes (slow chat/image calls), so the
# single-flight lock TTL must exceed worst-case ingest. With no heartbeat this
# also bounds how long a crashed ingest's lock lingers before recovery retries.
INGEST_RECOVERY_LOCK_TTL_MS = 30 * 60 * 1000


def ingest_active_lock_key(book_id: str) -> str:
    """Single-flight key shared by upload-triggered ingest and the recovery worker.

    Both the API (ingest-on-upload) and the recovery worker can target the same
    ``importing`` book; this lock makes them mutually exclusive so they never
    double-ingest and collide on the scenes/shots primary keys.
    """
    return f"kinora:ingest:active:{book_id}"


# --------------------------------------------------------------------------- #
# Director-mode seam results + protocols
# --------------------------------------------------------------------------- #


class CommentRoute(BaseModel):
    """How a Director region-comment was routed by the intent classifier (§5.4)."""

    agent: str
    aspect: str
    message: str


class RegenOutcome(BaseModel):
    """The result of regenerating one shot after a Director edit (§8.7)."""

    shot_id: str
    status: str
    oss_url: str | None = None
    qa: dict[str, object] | None = None


class CommentClassifier(Protocol):
    """Classify a Director note + bound shot to an agent route (the §5.4 router)."""

    async def classify(self, note: str, *, shot_context: str | None = None) -> CommentRoute: ...


#: ``run_regen(book_id, shot_id, session_id) -> RegenOutcome`` — render one shot.
RegenRunner = Callable[[str, str, str | None], Awaitable[RegenOutcome]]
#: ``run_ingest(book_id, pdf_bytes, session_id) -> None`` — Phase A for a book.
IngestRunner = Callable[[str, bytes, str | None], Awaitable[None]]


def make_session_factory(maker: async_sessionmaker[AsyncSession]) -> SessionFactory:
    """Build a committing unit-of-work factory bound to ``maker``.

    Mirrors :func:`app.db.session.get_session`: commit on clean exit, roll back on
    error. Repositories only ``flush``, so this boundary owns the transaction.
    """

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return factory


# --------------------------------------------------------------------------- #
# Default (real) comment classifier — a cheap Qwen chat() call (§5.4)
# --------------------------------------------------------------------------- #


class _ChatCommentClassifier:
    """Route a Director note with one cheap ``chat()`` call (kinora.md §5.4).

    "Make her coat red" -> Cinematographer (look) + Continuity; "too fast" ->
    Cinematographer (pacing); "wrong room" -> Continuity (room). The model picks
    one primary agent; deterministic keyword fallbacks keep it robust if the
    reply is unusable.
    """

    _SYSTEM = (
        "You route a film director's note to ONE agent. Reply with strict JSON: "
        '{"agent": "cinematographer"|"continuity", "aspect": "pacing"|"look"|'
        '"room"|"canon"|"composition", "message": "<one-line restatement>"}. '
        "Pacing/look/composition -> cinematographer. Wrong room/location, "
        "object/canon contradiction, character appearance continuity -> continuity."
    )

    def __init__(self, chat: object, model: str) -> None:
        self._chat = chat
        self._model = model

    async def classify(self, note: str, *, shot_context: str | None = None) -> CommentRoute:
        user = note if not shot_context else f"{note}\n\n[shot context: {shot_context}]"
        messages = [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": user},
        ]
        try:
            raw = await self._chat.chat_json(  # type: ignore[attr-defined]
                messages, self._model, temperature=0.0, max_tokens=200, stream=False
            )
        except Exception as exc:  # noqa: BLE001 - never fail a comment on classifier error
            logger.warning("director.classify_failed", error=str(exc))
            return self._fallback(note)
        if not isinstance(raw, dict):
            return self._fallback(note)
        agent = str(raw.get("agent", "")).lower().strip()
        aspect = str(raw.get("aspect", "")).lower().strip()
        if agent not in {"cinematographer", "continuity"}:
            return self._fallback(note)
        message = str(raw.get("message") or note)
        return CommentRoute(agent=agent, aspect=aspect or "look", message=message)

    @staticmethod
    def _fallback(note: str) -> CommentRoute:
        text = note.lower()
        if any(w in text for w in ("room", "place", "location", "where", "wrong", "should be")):
            return CommentRoute(agent="continuity", aspect="room", message=note)
        if any(w in text for w in ("fast", "slow", "pace", "pacing", "speed", "linger")):
            return CommentRoute(agent="cinematographer", aspect="pacing", message=note)
        return CommentRoute(agent="cinematographer", aspect="look", message=note)


# --------------------------------------------------------------------------- #
# The Container
# --------------------------------------------------------------------------- #


@dataclass
class Container:
    """Every wired subsystem, behind one typed handle stored on ``app.state``.

    Construction is pure (no I/O). Connections open on first use and are closed
    by :meth:`shutdown`.
    """

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    session_factory: SessionFactory
    redis: RedisClient
    object_store: ObjectStore
    queue: RedisRenderQueue
    render_enqueuer: RenderEnqueuer
    scheduler_store: SchedulerStore
    keyframe_maintainer: QueueKeyframeMaintainer
    budget_limits: BudgetLimits

    # -- overridable seams (None => lazily built real default) --------------- #
    embedder: Embedder | None = None
    shot_planner: ShotPlanner | None = None
    comment_classifier: CommentClassifier | None = None
    regen_runner: RegenRunner | None = None
    ingest_runner: IngestRunner | None = None
    # Billing payment provider seam (additive). None => the in-memory fake
    # transport is built lazily; tests can inject a fake with scripted failures.
    # A real Stripe transport is NEVER wired here.
    billing_provider: object | None = None

    # -- private lazy caches ------------------------------------------------- #
    _providers: Providers | None = field(default=None, repr=False)
    _tools: MemoryTools | None = field(default=None, repr=False)
    _keyframe_service: KeyframeService | None = field(default=None, repr=False)
    _billing_service: object | None = field(default=None, repr=False)
    _bg_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    # -- providers (lazy; constructing them needs the key but no network) ---- #

    @property
    def providers(self) -> Providers:
        """The shared DashScope provider bundle (constructed on first use)."""
        if self._providers is None:
            from app.providers import create_providers

            self._providers = create_providers(self.settings)
        return self._providers

    def _embedder(self) -> Embedder:
        return self.embedder if self.embedder is not None else self.providers.embeddings

    def _planner(self) -> ShotPlanner:
        if self.shot_planner is not None:
            return self.shot_planner
        from app.agents.adapter import Adapter

        # Bind the Adapter to THIS container's unit of work so ``shot.plan`` reads
        # the same database the rest of the container writes (not the global one).
        self.shot_planner = Adapter(
            self.providers, settings=self.settings, session_factory=self.session_factory
        )
        return self.shot_planner

    # -- the MCP tool layer: the single DI-seam satisfaction point ----------- #

    def build_tools(self) -> MemoryTools:
        """Build the §8.3 :class:`MemoryTools` with the REAL enqueuer + planner.

        ``shot.render`` enqueues through the real :class:`RedisRenderEnqueuer`;
        ``shot.plan`` runs the real :class:`Adapter`. Cached after first build.
        """
        if self._tools is None:
            from app.mcp.tools import MemoryTools

            self._tools = MemoryTools(
                embedder=self._embedder(),
                session_factory=self.session_factory,
                blob_store=self.object_store,
                limits=self.budget_limits,
                enqueuer=self.render_enqueuer,
                planner=self._planner(),
            )
        return self._tools

    def build_mcp_authorizer(self) -> BookScopedAuthorizer:
        """The §12 book-scoped authorizer for the streamable-HTTP MCP surface.

        Rejects tool calls naming an unknown ``book_id``; the lookup hits
        :class:`BookRepo` over a short-lived session.
        """
        from app.db.repositories.book import BookRepo
        from app.mcp.authz import BookScopedAuthorizer

        async def _book_exists(book_id: str) -> bool:
            async with self.session_factory() as db:
                return await BookRepo(db).get(book_id) is not None

        return BookScopedAuthorizer(book_exists=_book_exists)

    @property
    def keyframe_service(self) -> KeyframeService:
        """The cheap keyframe lane (image-gen stills, zero video-seconds, §4.4)."""
        if self._keyframe_service is None:
            from app.scheduler.keyframe import KeyframeService

            self._keyframe_service = KeyframeService(
                image=self.providers.image,
                object_store=self.object_store,
                redis=self.redis,
                settings=self.settings,
            )
        return self._keyframe_service

    # -- billing & payments (additive; commercial mirror of the §11 budget) -- #

    def _build_billing_provider(self) -> object:
        """The payment provider transport — ALWAYS the in-memory fake by default.

        No real Stripe/network/payment call is ever made. The ``billing_provider``
        seam lets tests inject a fake with scripted declines; production uses the
        same fake transport (the Stripe shape exists but is intentionally unwired).
        """
        if self.billing_provider is not None:
            return self.billing_provider
        from app.billing.provider.base import ProviderConfig
        from app.billing.provider.fake import FakePaymentProvider

        self.billing_provider = FakePaymentProvider(
            _config=ProviderConfig(
                name="fake",
                webhook_secret=self.settings.billing_webhook_secret,
                webhook_tolerance_s=self.settings.billing_webhook_tolerance_s,
            )
        )
        return self.billing_provider

    @property
    def billing_service(self) -> object:
        """The :class:`app.billing.service.BillingService` (lazy, fake provider)."""
        if self._billing_service is None:
            from app.billing.service import BillingConfig, BillingService
            from app.billing.tax import TaxRateResolver

            retry_days = tuple(
                int(p.strip())
                for p in str(self.settings.billing_dunning_retry_days).split(",")
                if p.strip()
            ) or (1, 3, 5, 7)
            self._billing_service = BillingService(
                session_factory=self.session_factory,
                provider=self._build_billing_provider(),  # type: ignore[arg-type]
                config=BillingConfig(
                    default_currency=self.settings.billing_default_currency,
                    invoice_prefix=self.settings.billing_invoice_prefix,
                    dunning_retry_days=retry_days,
                    auto_charge_on_finalize=self.settings.billing_auto_charge_on_finalize,
                ),
                tax_resolver=TaxRateResolver.with_defaults(),
            )
        return self._billing_service

    def build_billing_webhook_handler(self) -> object:
        """Build the idempotent inbound-webhook handler over the fake provider."""
        from app.billing.webhooks import WebhookHandler

        return WebhookHandler(
            service=self.billing_service,  # type: ignore[arg-type]
            provider=self._build_billing_provider(),  # type: ignore[arg-type]
        )

    # -- per-request scheduler stack (bound to a request DB session) --------- #

    def build_scheduler(self, session: AsyncSession) -> SchedulerService:
        """Build a :class:`SchedulerService` bound to ``session`` (budget + spans)."""
        from app.db.repositories.budget import BudgetRepo
        from app.db.repositories.shot import SourceSpanRepo
        from app.scheduler.events import RedisSessionEventPublisher

        return SchedulerService(
            queue=self.queue,
            budget=BudgetService(repo=BudgetRepo(session), limits=self.budget_limits),
            shots=SourceSpanRepo(session),
            keyframes=self.keyframe_maintainer,
            store=self.scheduler_store,
            settings=self.settings,
            events=RedisSessionEventPublisher(self.redis),
        )

    def build_intent_controller(self, session: AsyncSession) -> IntentController:
        """Build the §4.7/§4.8 :class:`IntentController` over a request session."""
        return IntentController(
            service=self.build_scheduler(session),
            store=self.scheduler_store,
            settings=self.settings,
        )

    # -- Director seams ------------------------------------------------------ #

    def _classifier(self) -> CommentClassifier:
        if self.comment_classifier is not None:
            return self.comment_classifier
        self.comment_classifier = _ChatCommentClassifier(
            self.providers.chat, self.settings.chat_model_adapter
        )
        return self.comment_classifier

    async def classify_comment(self, note: str, *, shot_context: str | None = None) -> CommentRoute:
        """Route a Director note to an agent (cheap chat() classifier, §5.4)."""
        return await self._classifier().classify(note, shot_context=shot_context)

    # -- preference learning (§8.6: every Director edit teaches a prior) ------ #

    async def record_note_prefs(
        self, note: str, *, user_id: str | None, book_id: str | None
    ) -> list[PreferencePrior]:
        """Learn directing priors from a Director region-comment (§8.6).

        Best-effort: a preference write must never break the comment's regen, so a
        failure is logged and swallowed.
        """
        from app.db.repositories.pref import PrefsRepo
        from app.memory.prefs_service import PrefsService

        try:
            async with self.session_factory() as db:
                priors = await PrefsService(prefs=PrefsRepo(db)).record_note(
                    note, user_id=user_id, book_id=book_id
                )
            return priors
        except Exception as exc:  # noqa: BLE001 - learning is best-effort
            logger.warning("prefs.record_note_failed", error=str(exc))
            return []

    async def record_edit_prefs(
        self, changes: dict[str, object], *, user_id: str | None, book_id: str | None
    ) -> list[PreferencePrior]:
        """Learn directing priors from a canon edit's changes (§8.6, best-effort)."""
        from app.db.repositories.pref import PrefsRepo
        from app.memory.prefs_service import PrefsService

        try:
            async with self.session_factory() as db:
                priors = await PrefsService(prefs=PrefsRepo(db)).record_changes(
                    changes, user_id=user_id, book_id=book_id
                )
            return priors
        except Exception as exc:  # noqa: BLE001 - learning is best-effort
            logger.warning("prefs.record_edit_failed", error=str(exc))
            return []

    async def get_prefs(
        self, *, user_id: str | None = None, book_id: str | None = None
    ) -> PreferencePriors:
        """Read aggregated directing priors for a scope (§8.6) — for the Settings panel."""
        from app.db.repositories.pref import PrefsRepo
        from app.memory.prefs_service import PrefsService

        async with self.session_factory() as db:
            return await PrefsService(prefs=PrefsRepo(db)).get(user_id=user_id, book_id=book_id)

    async def reset_prefs(self, *, user_id: str | None = None, book_id: str | None = None) -> int:
        """Clear learned directing priors for a scope; return how many were removed."""
        from app.db.repositories.pref import PrefsRepo
        from app.memory.prefs_service import PrefsService

        async with self.session_factory() as db:
            return await PrefsService(prefs=PrefsRepo(db)).reset(user_id=user_id, book_id=book_id)

    async def run_regen(
        self, book_id: str, shot_id: str, session_id: str | None = None
    ) -> RegenOutcome:
        """Regenerate one shot through the real render pipeline (or the seam)."""
        if self.regen_runner is not None:
            return await self.regen_runner(book_id, shot_id, session_id)
        return await self._default_run_regen(book_id, shot_id, session_id)

    async def _default_run_regen(
        self, book_id: str, shot_id: str, session_id: str | None
    ) -> RegenOutcome:
        from app.render.pipeline import build_render_pipeline

        async with self.session_factory() as db:
            pipeline = build_render_pipeline(
                db,
                providers=self.providers,
                object_store=self.object_store,
                settings=self.settings,
            )
            result = await pipeline.render_shot(book_id, shot_id, session_id=session_id)
        return RegenOutcome(
            shot_id=result.shot_id,
            status=result.status.value,
            oss_url=result.clip_url,
            qa=result.qa,
        )

    async def run_ingest(
        self, book_id: str, pdf_bytes: bytes, session_id: str | None = None
    ) -> None:
        """Run Phase A ingest for a book, publishing progress events (or the seam).

        Single-flight per book: the upload path ingests in-process while the
        recovery worker independently scans for ``importing`` rows, so without a
        shared lock both can ingest the same book at once and collide on the
        scenes/shots primary keys (UniqueViolation -> the book is marked failed).
        A Redis ``SET NX`` lock makes the two paths mutually exclusive.
        """
        if self.ingest_runner is not None:
            await self.ingest_runner(book_id, pdf_bytes, session_id)
            return
        lock = self.redis.lock(
            ingest_active_lock_key(book_id),
            ttl_ms=INGEST_RECOVERY_LOCK_TTL_MS,
            blocking=False,
        )
        if not await lock.acquire():
            logger.info("ingest.skip_active", book_id=book_id)
            return
        try:
            await self._default_run_ingest(book_id, pdf_bytes, session_id)
        finally:
            with suppress(Exception):
                await lock.release()

    async def _default_run_ingest(
        self, book_id: str, pdf_bytes: bytes, session_id: str | None
    ) -> None:
        import anyio

        from app.ingest.service import ingest_pdf
        from app.queue.redis_queue import book_progress_key
        from app.storage.object_store import keys

        channel = session_channel(session_id) if session_id else book_channel(book_id)
        owner_id: str | None = None
        async with self.session_factory() as db:
            from app.db.repositories.book import BookRepo

            book = await BookRepo(db).get(book_id)
            owner_id = book.user_id if book is not None else None
        lib_channel = library_channel(owner_id) if owner_id else None

        async def progress(stage: str, pct: float) -> None:
            snapshot = {"stage": stage, "pct": pct}
            message = {"event": "ingest_progress", "book_id": book_id, **snapshot}
            await self.redis.set_json(book_progress_key(book_id), snapshot)
            await self.redis.publish(channel, message)
            if lib_channel is not None:
                await self.redis.publish(lib_channel, message)

        # A publisher-supplied cover (written at upload for an EPUB that declares
        # one) becomes page 1's image; absent it, page 1 is the rendered page.
        cover_image: tuple[bytes, str] | None = None
        cover_key = keys.cover(book_id)
        if await anyio.to_thread.run_sync(self.object_store.exists, cover_key):
            from app.ingest.epub_extract import sniff_image_content_type

            cover_bytes = await anyio.to_thread.run_sync(self.object_store.get_bytes, cover_key)
            cover_image = (cover_bytes, sniff_image_content_type(cover_bytes))

        await ingest_pdf(
            book_id,
            pdf_bytes,
            providers=self.providers,
            blob_store=self.object_store,
            settings=self.settings,
            session_factory=self.session_factory,
            progress=progress,
            cover_image=cover_image,
        )

    # -- durable ingest recovery -------------------------------------------- #

    async def recover_importing_books(self, *, limit: int = INGEST_RECOVERY_LIMIT) -> int:
        """Respawn Phase-A ingest for books left ``importing`` after a restart.

        Upload already persists the normalized source PDF before creating the
        durable ``books`` row. This recovery path leans on that existing artifact:
        find stuck rows, reload ``source_pdf_key`` from object storage, and run
        the same ingest code path. A short Redis lock prevents duplicate recovery
        when several API instances boot together.
        """
        from app.db.repositories.book import BookRepo

        try:
            async with self.session_factory() as db:
                books = await BookRepo(db).list_by_status(BookStatus.IMPORTING, limit=limit)
        except Exception as exc:  # noqa: BLE001 - startup recovery is best-effort
            logger.warning("ingest.recovery.scan_failed", error=str(exc))
            return 0

        spawned = 0
        for book in books:
            if not book.source_pdf_key:
                logger.warning("ingest.recovery.missing_source", book_id=book.id)
                continue
            self.spawn(self._recover_ingest_book(book.id, book.source_pdf_key))
            spawned += 1
        if spawned:
            logger.info("ingest.recovery.spawned", count=spawned)
        return spawned

    async def _recover_ingest_book(self, book_id: str, pdf_key: str) -> None:
        # No separate lock here: run_ingest holds the shared single-flight lock,
        # so a book that is already being ingested (by an upload or another
        # recovery) is skipped inside run_ingest rather than double-ingested.
        try:
            pdf_bytes = await asyncio.to_thread(self.object_store.get_bytes, pdf_key)
            await self.run_ingest(book_id, pdf_bytes, None)
            logger.info("ingest.recovery.done", book_id=book_id)
        except Exception as exc:  # noqa: BLE001 - retain row for a later retry
            logger.warning("ingest.recovery.failed", book_id=book_id, error=str(exc))

    async def _startup_recover_importing_books(self) -> None:
        await self.recover_importing_books(limit=self.settings.ingest_recovery_limit)

    # -- targeted regen enqueue (Director comment / canon edit) -------------- #

    async def enqueue_regen(self, spec: ShotSpec, *, cancel_token: str | None = None) -> str:
        """Enqueue a single shot for committed regen via the real enqueuer (§8.7)."""
        return await self.render_enqueuer.enqueue(spec, RenderPriority.COMMITTED, cancel_token)

    # -- background tasks (ingest / regen fan-out) --------------------------- #

    def spawn(self, coro: Awaitable[None]) -> asyncio.Task[None]:
        """Run ``coro`` as a tracked background task (awaited/cancelled on shutdown)."""
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # -- readiness probe (the /ready gate, §12) ------------------------------ #

    async def check_readiness(self) -> dict[str, bool]:
        """Probe the critical dependencies for the readiness gate.

        Postgres via ``SELECT 1`` and Redis via ``PING``; each probe is guarded so
        it returns ``False`` (never raises) when its dependency is unreachable, so
        ``/ready`` can answer 503 rather than 500.
        """
        return {"postgres": await self._check_postgres(), "redis": await self._check_redis()}

    async def _check_postgres(self) -> bool:
        try:
            async with self.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - readiness probe must never raise
            logger.warning("readiness.postgres_down", error=str(exc))
            return False

    async def _check_redis(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except Exception as exc:  # noqa: BLE001 - readiness probe must never raise
            logger.warning("readiness.redis_down", error=str(exc))
            return False

    # -- lifecycle ----------------------------------------------------------- #

    async def startup(self) -> None:
        """Lifecycle hook. Connections are lazy, so this only logs readiness."""
        logger.info("container.startup", env=self.settings.app_env)
        self.spawn(self._startup_recover_importing_books())

    async def shutdown(self) -> None:
        """Cancel background tasks, close Redis, dispose the engine, close providers."""
        for task in list(self._bg_tasks):
            task.cancel()
        for task in list(self._bg_tasks):
            with suppress(asyncio.CancelledError, Exception):
                await task
        with suppress(Exception):
            await self.redis.close()
        if self._providers is not None:
            with suppress(Exception):
                await self._providers.aclose()
        with suppress(Exception):
            await self.engine.dispose()
        logger.info("container.shutdown")


def build_container(settings: Settings | None = None) -> Container:
    """Construct the fully-wired :class:`Container` from application settings.

    Pure construction: no sockets are opened here. The engine, Redis client, and
    object-store client are all lazy, so this is safe to call at import time and
    in ``create_app()`` without infrastructure or a live DashScope key.
    """
    settings = settings or get_settings()

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        future=True,
    )
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session_factory = make_session_factory(maker)

    redis = RedisClient.from_url(settings.redis_url)
    object_store = ObjectStore.from_settings(settings)
    queue = RedisRenderQueue(
        redis,
        retry_cap=settings.retry_cap,
        session_factory=session_factory,
    )
    render_enqueuer: RenderEnqueuer = RedisRenderEnqueuer(queue)
    scheduler_store = SchedulerStore(redis, session_factory=session_factory)
    keyframe_maintainer = QueueKeyframeMaintainer(queue)

    return Container(
        settings=settings,
        engine=engine,
        sessionmaker=maker,
        session_factory=session_factory,
        redis=redis,
        object_store=object_store,
        queue=queue,
        render_enqueuer=render_enqueuer,
        scheduler_store=scheduler_store,
        keyframe_maintainer=keyframe_maintainer,
        budget_limits=BudgetLimits.from_settings(settings),
    )


__all__ = [
    "CommentClassifier",
    "CommentRoute",
    "Container",
    "IngestRunner",
    "RegenOutcome",
    "RegenRunner",
    "SessionFactory",
    "build_container",
    "make_session_factory",
]
