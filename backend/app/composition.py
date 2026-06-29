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
from typing import TYPE_CHECKING, Any, Protocol

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
from app.finops.tiers import BudgetTierPolicy
from app.memory.budget_service import BudgetLimits, BudgetService
from app.memory.interfaces import Embedder, RenderEnqueuer, ShotPlanner, ShotSpec
from app.moderation.service import ModerationFactory, ModerationService
from app.queue.enqueuer import RedisRenderEnqueuer
from app.queue.redis_queue import RedisRenderQueue, book_channel, library_channel, session_channel
from app.redis.client import RedisClient
from app.scheduler.intent import IntentController
from app.scheduler.model import SchedulerStore
from app.scheduler.service import QueueKeyframeMaintainer, SchedulerService
from app.storage.object_store import ObjectStore

if TYPE_CHECKING:
    from app.analytics.service import AnalyticsService
    from app.analytics.sink import SummarySink
    from app.analytics.store import AnalyticsStore
    from app.assistant.memory import ConversationMemory
    from app.assistant.service import AssistantService
    from app.assistant.synth import ChatClient
    from app.auth.service import AuthService
    from app.eventsourcing.store.service import EventStoreFactory
    from app.finops.service import FinOpsService
    from app.flags.service import FlagService
    from app.integrations.http import HttpxClient
    from app.integrations.service import IntegrationsService
    from app.mcp.authz import BookScopedAuthorizer
    from app.mcp.tools import MemoryTools
    from app.media.service import MediaService
    from app.memory.prefs_service import PreferencePrior, PreferencePriors
    from app.notifications.service import NotificationService
    from app.providers import Providers
    from app.recommendations.store import RecommendationService
    from app.scheduler.keyframe import KeyframeService
    from app.search.alias import AliasRegistry
    from app.search.index import SearchIndex as SearchIndexProto
    from app.search.pipeline import IndexingPipeline
    from app.search.service import SearchService
    from app.translation.provider import TranslationProvider
    from app.translation.service import TranslationService
else:
    SearchIndexProto = object

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
    finops_policy: BudgetTierPolicy

    # -- overridable seams (None => lazily built real default) --------------- #
    embedder: Embedder | None = None
    shot_planner: ShotPlanner | None = None
    comment_classifier: CommentClassifier | None = None
    regen_runner: RegenRunner | None = None
    ingest_runner: IngestRunner | None = None
    #: Third-party integrations facade (lazily built; overridable in tests).
    integrations: IntegrationsService | None = None
    # The server-side search index (app/search). None => lazily built per
    # ``settings.search_backend`` (postgres FTS+pgvector hybrid, or in-memory).
    search_index: SearchIndexProto | None = None

    # -- overridable translation seam (None => lazily built real default) ---- #
    # The content-translation provider (app.translation). A test can inject a
    # FakeTranslationProvider here so the whole subsystem runs with zero live
    # calls; production lazily builds the LLM-backed provider on the shared chat
    # seam. Additive: nothing else in the container depends on it.
    translation_provider: TranslationProvider | None = None

    #: Content-moderation factory (app.moderation). Overridable seam: tests inject
    #: one built on the deterministic keyword classifier so no model is ever
    #: called. ``None`` => lazily built from providers (or the keyword fake when
    #: providers are unavailable). See :meth:`build_moderation`.
    moderation_factory: ModerationFactory | None = None

    #: Event-sourcing event-store factory (app.eventsourcing.store). Overridable
    #: seam: tests inject one with a custom serializer/registry; ``None`` => lazily
    #: built from :class:`Settings`. Infra-free to construct (the lazy-composition
    #: rule); session-bound stores are produced per unit of work. The domain &
    #: projection facets consume the protocols it builds. See :meth:`build_event_store`.
    event_store_factory: EventStoreFactory | None = None

    # Billing payment provider seam (additive). None => the in-memory fake
    # transport is built lazily; tests can inject a fake with scripted failures.
    # A real Stripe transport is NEVER wired here.
    billing_provider: object | None = None
    analytics_store: AnalyticsStore | None = None
    analytics_summary_sink_seam: SummarySink | None = None

    #: Chat client the reader assistant synthesizes over (None => providers.chat).
    #: A seam so the assistant route can be driven end-to-end with a fake chat
    #: (zero credits) while production uses the real DashScope/OpenAI provider.
    assistant_chat: ChatClient | None = None

    # -- overridable seams (notifications platform) -------------------------- #
    #: Inject a fully-built service (tests use the in-memory default); when None a
    #: DB-backed one is built lazily from ``session_factory`` on first use.
    notification_service: NotificationService | None = None

    # -- private lazy caches ------------------------------------------------- #
    _providers: Providers | None = field(default=None, repr=False)
    _tools: MemoryTools | None = field(default=None, repr=False)
    _keyframe_service: KeyframeService | None = field(default=None, repr=False)
    _integrations_http: HttpxClient | None = field(default=None, repr=False)
    _auth_service: AuthService | None = field(default=None, repr=False)
    _translation_provider: TranslationProvider | None = field(default=None, repr=False)
    _billing_service: object | None = field(default=None, repr=False)
    _flag_service: FlagService | None = field(default=None, repr=False)
    _analytics_service: AnalyticsService | None = field(default=None, repr=False)
    _conversation_memory: ConversationMemory | None = field(default=None, repr=False)
    _llmops: Any = field(default=None, repr=False)
    _dataset_service: Any = field(default=None, repr=False)
    _media_service: MediaService | None = field(default=None, repr=False)
    #: Inference request router (app.inference.router). Additive lazy seam: a
    #: high-throughput LLM-inference scheduling brain (continuous batching, fair
    #: share, admission/backpressure, KV-affinity) over a network-free EchoBackend
    #: by default, so building it spends no credits (KINORA_LIVE_VIDEO stays OFF).
    #: Nothing else in the container depends on it; see :meth:`inference_router`.
    _inference_router: Any = field(default=None, repr=False)
    _bg_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    # -- providers (lazy; constructing them needs the key but no network) ---- #

    @property
    def providers(self) -> Providers:
        """The shared DashScope provider bundle (constructed on first use)."""
        if self._providers is None:
            from app.providers import create_providers

            self._providers = create_providers(self.settings)
        return self._providers

    # -- inference router (lazy; additive; network-free by default, §11/§12) -- #

    @property
    def inference_router(self) -> Any:
        """The multi-model inference request router (constructed on first use).

        A high-throughput LLM-inference *scheduling brain* (continuous batching,
        priority + weighted-fair-share across tenants/agents, admission +
        backpressure + queue-time SLAs, KV-cache-affinity routing, request
        coalescing) over the Kinora model stack (§11). Built over the
        network-free ``EchoBackend`` by default, so constructing it spends no
        credits and runs under ``DASHSCOPE_API_KEY=test`` with no network
        (``KINORA_LIVE_VIDEO`` stays OFF). A caller that wants real transport
        swaps in a ``ChatProviderBackend`` over :attr:`providers` per model.

        Additive: nothing else in the container depends on this seam.
        """
        if self._inference_router is None:
            from app.inference.router import build_multi_model_router

            # One router per crew model (§11): orchestration/high-volume/vision.
            self._inference_router = build_multi_model_router(
                {
                    self.settings.chat_model_max: 1,
                    self.settings.chat_model_plus: 2,
                    self.settings.vl_model: 2,
                }
            )
        return self._inference_router

    # -- auth & security plane (lazy; DB + Redis only, no providers, §6/§12) -- #

    @property
    def auth_service(self) -> AuthService:
        """The production auth orchestrator (constructed on first use).

        Composes the pluggable password hasher, the JWT/refresh token service
        (backed by the Redis access-token revocation store), the per-IP login
        throttle, and the auth repositories. Pure DB + Redis — no DashScope — so
        it works under the offline test harness and the ``DASHSCOPE_API_KEY=test``
        boot path.
        """
        if self._auth_service is None:
            from app.auth.lockout import LoginThrottle, RevocationStore
            from app.auth.service import AuthService
            from app.auth.tokens import TokenService
            from app.core.security import build_password_hasher

            revocations = RevocationStore(self.redis)
            hasher = build_password_hasher(
                self.settings.password_hasher, rounds=self.settings.bcrypt_rounds
            )
            self._auth_service = AuthService(
                settings=self.settings,
                session_factory=self.session_factory,
                hasher=hasher,
                tokens=TokenService(self.settings, revocations=revocations),
                throttle=LoginThrottle(
                    self.redis,
                    max_attempts=self.settings.login_ip_max_attempts,
                    window_s=self.settings.login_ip_window_s,
                ),
                revocations=revocations,
            )
        return self._auth_service

    @property
    def flag_service(self) -> FlagService:
        """The feature-flags & experimentation service (lazy; needs no network).

        Bound to THIS container's unit of work + Redis so flag reads/writes hit
        the same database and the cache invalidations ride the same Redis the
        rest of the container uses. The pure evaluator inside it works even with
        no infra; the service merely persists/caches definitions.
        """
        if self._flag_service is None:
            from app.flags.service import FlagService

            self._flag_service = FlagService(
                self.session_factory,
                redis=self.redis,
                default_salt=self.settings.flags_default_salt,
                cache_ttl_s=self.settings.flags_cache_ttl_s,
                channel=self.settings.flags_stream_channel,
            )
        return self._flag_service

    def _embedder(self) -> Embedder:
        return self.embedder if self.embedder is not None else self.providers.embeddings

    # -- LLM-ops platform (lazy; pure + offline — app.llmops) ---------------- #

    @property
    def llmops(self) -> Any:
        """The wired :class:`~app.llmops.service.LLMOpsService` (built on first use).

        Pure + offline: it seeds the prompt registry from ``app.agents.prompts``
        and builds an in-memory trace store + response cache + model catalog +
        deterministic judge, sized from the ``llmops_*`` settings. Constructing it
        makes no model call and opens no connection — it is additive and inert
        until a route (or a caller) reaches for it.
        """
        if self._llmops is None:
            from app.llmops.cache import InMemoryBackend, ResponseCache
            from app.llmops.guardrails import GuardrailPolicy
            from app.llmops.output_policy import OutputPolicy, Severity
            from app.llmops.service import LLMOpsService
            from app.llmops.tracing import InMemoryTraceStore

            s = self.settings
            guardrails = GuardrailPolicy(
                output_policy=OutputPolicy(expect_json=True, block_at=Severity.HIGH),
                input_block_score=s.llmops_injection_block_score,
                always_sanitize_input=s.llmops_guardrail_always_sanitize,
            )
            self._llmops = LLMOpsService.create(
                trace_store=InMemoryTraceStore(capacity=s.llmops_trace_capacity),
                cache=ResponseCache(
                    backend=InMemoryBackend(max_entries=s.llmops_cache_max_entries),
                    default_ttl_s=s.llmops_cache_ttl_s,
                ),
                guardrails=guardrails,
            )
        return self._llmops

    # -- ML-data platform (lazy; pure + offline — app.mlplatform.datasets) --- #

    @property
    def dataset_service(self) -> Any:
        """The wired :class:`~app.mlplatform.datasets.service.DatasetService`.

        Facet A of the self-improvement ML platform: it ingests agent run-traces
        through a read-only :class:`TraceSource` seam into a versioned, immutable
        dataset store (dedup, PII scrub, leak-free splitting, weak-supervision
        labels, stats / drift / diff, JSONL+columnar export). Pure + offline —
        constructing it makes no model call and opens no connection (an in-memory
        version registry), so it is additive and inert until a caller reaches for
        it. The :mod:`~app.mlplatform.datasets.sources.LLMOpsTraceSource` adapts
        ``self.llmops``'s trace store when a build is requested.
        """
        if self._dataset_service is None:
            from app.mlplatform.datasets.service import DatasetService

            self._dataset_service = DatasetService()
        return self._dataset_service

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

    # -- content moderation (app.moderation, §9/§10) ------------------------- #

    def _moderation_factory(self) -> ModerationFactory:
        """The process-wide moderation factory (constructed lazily on first use).

        Builds the model-backed classifier from the providers when available, else
        the deterministic keyword fake — so a no-key / no-network boot (and the
        health check) never forces a provider dependency. Tests override
        :attr:`moderation_factory` with a keyword-backed factory directly.
        """
        if self.moderation_factory is None:
            self.moderation_factory = ModerationFactory(
                providers=self.providers, settings=self.settings
            )
        return self.moderation_factory

    def build_moderation(self, session: AsyncSession) -> ModerationService:
        """Build a session-bound :class:`ModerationService` (the gate façade)."""
        return self._moderation_factory().build(session)

    # -- event sourcing: the event store (app.eventsourcing.store, facet A) --- #

    def event_store(self) -> EventStoreFactory:
        """The process-wide event-store factory (constructed lazily on first use).

        Built from :class:`Settings` (snapshot cadence + outbox tuning) unless a
        test injected one. Constructing it touches no infrastructure, so the
        health check / no-network boot is unaffected.
        """
        if self.event_store_factory is None:
            from app.eventsourcing.store.service import EventStoreFactory

            self.event_store_factory = EventStoreFactory.from_settings(self.settings)
        return self.event_store_factory

    def build_event_store(self, session: AsyncSession) -> object:
        """Build a session-bound :class:`PostgresEventStore` (the append/read seam).

        Returned typed as ``object`` to keep the Postgres/ORM import lazy at the
        composition root; callers in the eventsourcing facets import the concrete
        protocol type. The store only flushes — the caller's unit of work commits.
        """
        return self.event_store().store(session)

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

    def build_mcp_catalog(self) -> Any:
        """The versioned, scoped tool catalog for the MCP protocol layer (§8.3).

        The single source of tool metadata (version + scope tags + output schema)
        that the protocol server, validator, capabilities and client all share.
        """
        from app.mcp.registry import default_catalog

        return default_catalog()

    def build_mcp_resource_provider(self) -> Any:
        """The canon :class:`ResourceProvider` over the deployed tools (§8 resources).

        Reads canon resources through the same :class:`MemoryTools` (the single
        execution path), so a resource read is the same validated/authorized path
        as a tool call.
        """
        from app.mcp.resources import ResourceProvider

        return ResourceProvider(self.build_tools())

    def build_mcp_identity_resolver(self) -> Any:
        """The per-client identity resolver from the configured token→grant table (§12).

        Anonymous fallback is allowed only in ``local`` *and* only when no token
        table is configured — issuing scoped tokens locks the surface down.
        """
        from app.mcp.identity import StaticIdentityResolver

        return StaticIdentityResolver.from_config(
            self.settings.mcp_client_scopes or None,
            allow_anonymous=self.settings.is_local,
        )

    def build_scoped_authorizer(self) -> Any:
        """The §12 scope-enforcing authorizer, chained after the book-existence check.

        Composes :class:`ScopedAuthorizer` (per-client scope + book allowlist)
        with the existing :class:`BookScopedAuthorizer` (book existence). The
        caller binds it to an identity per session via ``for_identity``.
        """
        from app.mcp.identity import ScopedAuthorizer

        book_authz = self.build_mcp_authorizer()
        return ScopedAuthorizer(
            catalog=self.build_mcp_catalog(),
            next_authorizer=book_authz.authorize,
        )

    def build_recommendation_service(self, session: AsyncSession) -> RecommendationService:
        """Build the server-side recsys service bound to ``session`` (additive seam).

        The recommendations engine (``app.recommendations``) ranks which *books* a
        reader should watch next from the recsys warehouse (book_interactions /
        book_features / user_taste_vectors). Pure-math core; this seam only does
        the per-request I/O binding, mirroring :meth:`build_scheduler`.
        """
        from app.recommendations.engine import make_config_from_settings
        from app.recommendations.store import RecommendationService

        return RecommendationService(
            session, config=make_config_from_settings(self.settings)
        )

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

    # -- third-party integrations (lazy; overridable seam) ------------------- #

    def build_integrations(self) -> IntegrationsService:
        """Build the §9.1-import integrations facade with the real ingest seam.

        The facade is wired with a :class:`HttpxClient` (the one network seam),
        the configured token sealer, an OAuth-config resolver over settings, and
        a :class:`_KinoraIngestGateway` that creates books + spawns Phase-A ingest
        exactly like ``POST /books`` does. Cached after first build; tests inject
        a pre-built :attr:`integrations` instead.
        """
        if self.integrations is None:
            from app.integrations.backoff import BackoffPolicy
            from app.integrations.crypto import TokenSealer
            from app.integrations.http import HttpxClient
            from app.integrations.oauth import OAuth2Config
            from app.integrations.registry import default_registry
            from app.integrations.service import IntegrationsService

            self._integrations_http = HttpxClient()
            s = self.settings

            def oauth_config(provider: str) -> OAuth2Config | None:
                if provider == "notion" and s.notion_oauth_client_id:
                    return OAuth2Config(
                        provider="notion",
                        client_id=s.notion_oauth_client_id or "",
                        client_secret=s.notion_oauth_client_secret or "",
                        authorize_endpoint="https://api.notion.com/v1/oauth/authorize",
                        token_endpoint="https://api.notion.com/v1/oauth/token",
                        redirect_uri=s.integrations_oauth_redirect_uri,
                        extra_authorize_params={"owner": "user"},
                    )
                if provider == "pocket" and s.pocket_oauth_client_id:
                    return OAuth2Config(
                        provider="pocket",
                        client_id=s.pocket_oauth_client_id or "",
                        client_secret=s.pocket_oauth_client_secret or "",
                        authorize_endpoint="https://getpocket.com/auth/authorize",
                        token_endpoint="https://getpocket.com/v3/oauth/authorize",
                        redirect_uri=s.integrations_oauth_redirect_uri,
                    )
                return None

            self.integrations = IntegrationsService(
                session_factory=self.session_factory,
                ingest=_KinoraIngestGateway(self),
                http=self._integrations_http,
                sealer=TokenSealer(s.integrations_encryption_key),
                registry=default_registry(),
                oauth_config=oauth_config,
                backoff=BackoffPolicy(),
                max_items_per_sync=s.integrations_max_items_per_sync,
                error_threshold=s.integrations_error_threshold,
            )
        return self.integrations

    # -- server-side search engine (app/search, kinora.md §8) ---------------- #

    def build_search_index(self) -> SearchIndexProto:
        """The pluggable search index per ``settings.search_backend``.

        ``postgres`` => :class:`PostgresIndex` (FTS + pgvector hybrid over
        ``search_documents``) bound to this container's unit of work; ``memory``
        => the in-memory engine. Cached on ``search_index`` (also the test seam).
        """
        if self.search_index is None:
            if self.settings.search_backend.lower() == "memory":
                from app.search.memory_backend import InMemoryIndex

                self.search_index = InMemoryIndex()
            else:
                from app.search.postgres_backend import PostgresIndex

                self.search_index = PostgresIndex(
                    self.session_factory,
                    index_version=self.settings.search_default_version,
                )
        return self.search_index

    def search_alias_registry(self) -> AliasRegistry:
        """The alias→version registry for the versioned-index swap (reindex)."""
        if self.settings.search_backend.lower() == "memory":
            from app.search.alias import InMemoryAliasRegistry

            return InMemoryAliasRegistry(
                {self.settings.search_alias: self.settings.search_default_version}
            )
        from app.search.alias import PostgresAliasRegistry

        return PostgresAliasRegistry(self.session_factory)

    def build_search_pipeline(self) -> IndexingPipeline:
        """The indexing pipeline (project canon/library rows → the index)."""
        from app.search.pipeline import IndexingPipeline

        return IndexingPipeline(session_factory=self.session_factory, embedder=self._embedder())

    def build_search_service(self) -> SearchService:
        """The orchestration service (parse → embed → search → suggest)."""
        from app.search.service import SearchService

        return SearchService(self.build_search_index(), embedder=self._embedder())

    async def resolve_search_index(self) -> SearchIndexProto:
        """Resolve the live search index version from the alias (Postgres backend).

        For the Postgres backend this points the index at whatever version the
        alias currently names (so reads follow a reindex swap); for the in-memory
        backend it returns the single in-process index unchanged.
        """
        index = self.build_search_index()
        if self.settings.search_backend.lower() == "memory":
            return index
        from app.search.postgres_backend import PostgresIndex

        if isinstance(index, PostgresIndex):
            version = await self.search_alias_registry().resolve(self.settings.search_alias)
            if version and version != index.index_version:
                self.search_index = index.for_version(version)
                return self.search_index
        return index

    # -- content translation (app.translation; token-only, never video) ----- #

    def _get_translation_provider(self) -> TranslationProvider:
        """The translation provider seam (injected override > lazy LLM default).

        Lazily builds the LLM-backed provider on the shared chat seam (so it
        inherits the resilient transport + cost sink), unless an override was
        injected — a test injects a ``FakeTranslationProvider`` for zero live
        calls.
        """
        if self.translation_provider is not None:
            return self.translation_provider
        if self._translation_provider is None:
            from app.translation.llm_provider import make_llm_provider_from_providers

            self._translation_provider = make_llm_provider_from_providers(
                self.providers, model=self.settings.translation_model
            )
        return self._translation_provider

    def build_translation_service(
        self, *, glossary: object | None = None, memory: object | None = None
    ) -> TranslationService:
        """Build a :class:`TranslationService` with the configured provider.

        ``glossary`` is an optional :class:`~app.translation.glossary.Glossary`
        (e.g. hydrated from a book's canon character names + persisted terms);
        ``memory`` is an optional :class:`~app.translation.memory_store.TranslationMemory`
        (the API layer hydrates one from the DB before translating, so prior
        translations are served as zero-cost cache hits, §8.7). When omitted a
        fresh in-process memory is created.
        """
        from app.translation.glossary import Glossary
        from app.translation.memory_store import TranslationMemory
        from app.translation.service import TranslationService

        return TranslationService(
            self._get_translation_provider(),
            glossary=glossary if isinstance(glossary, Glossary) else None,
            memory=memory if isinstance(memory, TranslationMemory) else None,
            review_threshold=self.settings.translation_review_threshold,
        )

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

    # -- notifications platform (durable out-of-band notifications, §5/§12) -- #

    @property
    def notifications(self) -> NotificationService:
        """The notifications & webhooks platform (built lazily, DB-backed).

        Maps domain events (book ready, render done, budget low, conflict
        surfaced) onto templated, localized notifications and delivers them over
        in-app / email / push / signed-webhook channels with §12-grade reliability
        (idempotent outbox, backoff retries, circuit breaking, dead-letter, status
        tracking). Transports default to the no-network logging transports unless
        overridden, so this never spends credits or sends real mail in tests.
        """
        if self.notification_service is None:
            from app.notifications.factory import build_notification_service

            self.notification_service = build_notification_service(
                self.session_factory, settings=self.settings, log=logger.info
            )
        return self.notification_service

    async def notify_event(
        self,
        event: str,
        *,
        user_id: str,
        email: str | None = None,
        data: dict[str, object] | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        dedup_key: str | None = None,
    ) -> None:
        """Best-effort domain-event → notification hook (never breaks the caller).

        Resolves the recipient + their preferences and fans the event out across
        their opted-in channels. A notification failure must never break the
        domain action that triggered it (ingest finishing, a render completing),
        so the whole thing is guarded and logged.
        """
        from app.notifications.events import DomainEvent
        from app.notifications.models import Recipient

        try:
            domain_event = DomainEvent(event)
        except ValueError:
            logger.warning("notifications.unknown_event", event_name=event)
            return
        recipient = Recipient(user_id=user_id, email=email)
        try:
            await self.notifications.emit(
                domain_event,
                recipient=recipient,
                data=data or {},
                book_id=book_id,
                session_id=session_id,
                dedup_key=dedup_key,
            )
        except Exception as exc:  # noqa: BLE001 - notifications are best-effort
            logger.warning("notifications.emit_failed", event_name=event, error=str(exc))

    # -- media / asset service (app.media) — additive, lazy (Media domain) --- #

    @property
    def media_service(self) -> MediaService:
        """Content-addressed media store + ffmpeg derivations + GC (§8.7, §9)."""
        if self._media_service is None:
            from app.media.service import build_media_service

            self._media_service = build_media_service(
                self.settings,
                object_store=self.object_store,
                session_factory=self.session_factory,
            )
        return self._media_service

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

    def build_finops(self, session: AsyncSession) -> FinOpsService:
        """Build the §11.1 :class:`FinOpsService` over a request session.

        Composes the same :class:`BudgetService` contract the scheduler/pipeline
        use with the tenant cap, the USD cost ledger, tier alerts, forecasting,
        the quality↔budget optimizer, and reconciliation. Bound to ``session``.
        """
        from app.db.repositories.book import BookRepo
        from app.db.repositories.budget import BudgetRepo
        from app.db.repositories.finops import CostLedgerRepo
        from app.finops.service import FinOpsService

        return FinOpsService(
            budget_repo=BudgetRepo(session),
            cost_repo=CostLedgerRepo(session),
            book_repo=BookRepo(session),
            limits=self.budget_limits,
            policy=self.finops_policy,
            settings=self.settings,
        )

    # -- reader assistant: grounded RAG Q&A over a book + canon (§8 read side) -- #

    @property
    def conversation_memory(self) -> ConversationMemory:
        """Shared, Redis-backed conversation memory for the reader assistant (§8).

        Threaded follow-ups persist across requests (and API instances) through a
        per-conversation Redis key with a TTL. Built lazily so a container with no
        Redis use stays connection-free.
        """
        if self._conversation_memory is None:
            from app.assistant.memory import ConversationMemory, RedisConversationStore

            self._conversation_memory = ConversationMemory(
                RedisConversationStore(self.redis)
            )
        return self._conversation_memory

    def build_assistant(self, session: AsyncSession) -> AssistantService:
        """Build an :class:`AssistantService` bound to a request DB session.

        Wires the real canon read model (pages/entities/shots/beats over
        ``session``), the container's embedder seam (faked in tests), and the
        shared chat provider behind the synthesizer. The conversation memory is
        the container-level Redis-backed singleton so follow-ups thread.
        """
        from app.assistant.read_model import DbCanonReadModel
        from app.assistant.service import AssistantService
        from app.assistant.synth import AnswerSynthesizer

        read_model = DbCanonReadModel(session)
        chat = self.assistant_chat if self.assistant_chat is not None else self.providers.chat
        synthesizer = AnswerSynthesizer(chat, self.settings.chat_model_plus)
        return AssistantService(
            read_model,
            synthesizer,
            embedder=self._embedder(),
            memory=self.conversation_memory,
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

    # -- product analytics (app/analytics/, additive seam) ------------------- #

    def analytics_service(self) -> AnalyticsService:
        """Build the product-analytics façade over the configured store (§13/§5).

        Uses the injected :attr:`analytics_store` seam when set (tests pass an
        in-memory store); otherwise the real Postgres-backed store over this
        container's unit of work. Cached after first build. Distinct from the
        ops-observability metrics and the §13 eval warehouse — this is the
        human-usage event pipeline.
        """
        if self._analytics_service is None:
            from datetime import timedelta

            from app.analytics.service import AnalyticsService
            from app.analytics.store import AnalyticsStore

            store: AnalyticsStore
            if self.analytics_store is not None:
                store = self.analytics_store
            else:
                from app.analytics.store_pg import PostgresAnalyticsStore

                store = PostgresAnalyticsStore(self.session_factory)
            assert isinstance(store, AnalyticsStore)  # narrow for the runtime import
            self._analytics_service = AnalyticsService(
                store,
                salt=self.settings.analytics_salt_effective,
                session_gap=timedelta(seconds=self.settings.analytics_session_gap_s),
                max_batch=self.settings.analytics_max_batch,
            )
        return self._analytics_service

    def analytics_summary_sink(self) -> SummarySink:
        """The summary sink the rollup worker persists into (Postgres or seam)."""
        if self.analytics_summary_sink_seam is not None:
            return self.analytics_summary_sink_seam
        from app.analytics.sink_pg import PostgresSummarySink

        return PostgresSummarySink(self.session_factory)

    async def run_analytics_rollup(self) -> dict[str, int]:
        """Re-aggregate the trailing analytics window into the summary tables.

        Folds the configured look-back window into per-granularity rollups + the
        derived-session upsert, idempotently. Driven by the analytics rollup
        worker (and callable ad-hoc). Returns the row counts written.
        """
        from datetime import UTC, datetime, timedelta

        from app.analytics.timebucket import Granularity

        now = datetime.now(UTC)
        since = now - timedelta(days=self.settings.analytics_rollup_window_days)
        result = await self.analytics_service().run_rollup_job(
            self.analytics_summary_sink(),
            since=since,
            until=now,
            granularities=(Granularity.DAY, Granularity.WEEK),
        )
        return {
            "events": result.events,
            "rollup_rows": result.rollup_rows,
            "session_rows": result.session_rows,
        }

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

    def start_notification_bridge(self) -> asyncio.Task[None] | None:
        """Spawn the live-event → durable-notification bridge as a tracked task.

        Called from the API lifespan (alongside the idle-sweeper) so it is gated
        the same way background work is — tests that disable background tasks never
        start it, which keeps the timing-sensitive worker/pubsub tests isolated.
        Returns the task (or ``None`` when disabled by settings).
        """
        if not getattr(self.settings, "notify_bridge_enabled", False):
            return None
        return self.spawn(self._run_notification_bridge())

    async def _run_notification_bridge(self) -> None:
        """Subscribe to the live §5.6 channels and emit durable notifications.

        Purely a consumer of the existing event bus — it never touches the
        publishers — so adding it is additive. Fully guarded: a failure to start
        (e.g. Redis down at boot) is logged and the API runs without it.
        """
        try:
            from app.notifications.factory import build_notification_bridge

            bridge = build_notification_bridge(
                self.redis, self.notifications, self.session_factory, log=logger.info
            )
            await bridge.run_pattern("kinora:events:*")
        except Exception as exc:  # noqa: BLE001 - the bridge must never crash the app
            logger.warning("notifications.bridge.start_failed", error=str(exc))

    async def shutdown(self) -> None:
        """Cancel background tasks, close Redis, dispose the engine, close providers."""
        for task in list(self._bg_tasks):
            task.cancel()
        for task in list(self._bg_tasks):
            with suppress(asyncio.CancelledError, Exception):
                await task
        with suppress(Exception):
            await self.redis.close()
        if self._integrations_http is not None:
            with suppress(Exception):
                await self._integrations_http.aclose()
        if self._providers is not None:
            with suppress(Exception):
                await self._providers.aclose()
        with suppress(Exception):
            await self.engine.dispose()
        logger.info("container.shutdown")


class _KinoraIngestGateway:
    """The integrations → §9.1 ingest seam, mirroring ``POST /books``.

    Given rendered PDF bytes from an imported source item, it creates the
    ``importing`` book row (owned by the reader), persists the PDF under the
    canonical ``pdfs/`` key, and spawns Phase-A ingest out-of-band — the same
    durable path a manual upload takes. Returns the new book id.
    """

    def __init__(self, container: Container) -> None:
        self._c = container

    async def import_pdf(
        self,
        *,
        user_id: str,
        title: str,
        author: str | None,
        pdf_bytes: bytes,
        source: str,
    ) -> str:
        import anyio

        from app.db.base import new_id
        from app.db.repositories.book import BookRepo
        from app.queue.redis_queue import book_progress_key
        from app.storage.object_store import keys

        book_id = new_id()
        pdf_key = keys.pdf(book_id)
        await anyio.to_thread.run_sync(
            self._c.object_store.put_bytes, pdf_key, pdf_bytes, "application/pdf"
        )
        async with self._c.session_factory() as session:
            await BookRepo(session).create(
                title=title[:512] or "Untitled",
                author=(author or None),
                user_id=user_id,
                source_pdf_key=pdf_key,
                status=BookStatus.IMPORTING,
                art_direction=None,
                book_id=book_id,
            )
        await self._c.redis.set_json(
            book_progress_key(book_id), {"stage": "importing", "pct": 0.0}
        )
        # Phase A out-of-band — the imported book becomes a first-class book.
        self._c.spawn(self._c.run_ingest(book_id, pdf_bytes, None))
        logger.info("integrations.import.book_created", book_id=book_id, source=source)
        return book_id


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
        finops_policy=BudgetTierPolicy.from_settings(settings),
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
