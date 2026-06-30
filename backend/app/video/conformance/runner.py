"""The programmatic conformance runner: ``run_conformance(provider) -> Report``.

This is the engine the pytest helper (:mod:`.suite`) and the CLI
(:mod:`.__main__`) both call. It runs every :class:`ConformanceCheck` against an
adapter and returns a scored :class:`ConformanceReport`. Each check is a small
function that drives the adapter through a deterministic
:class:`~app.video.conformance.transport.ScriptedTransport` profile and asserts
one guarantee:

* **capability honesty** — every *declared* mode/duration/resolution renders;
  every *undeclared* one is rejected with a non-retryable
  :class:`ProviderBadRequest`.
* **request mapping** — a canonical :class:`WanSpec` round-trips through the
  adapter's native request body without losing prompt / seed / negative-prompt /
  conditioning-URL fields.
* **error taxonomy** — each transport fault (4xx / 401 / 429 / 5xx / timeout)
  surfaces as the matching shared error class, with retryability intact.
* **asset handling** — ``render`` returns real bytes and eagerly downloads the
  expiring URL (never hands back a bare URL).
* **last frame** — a continuation-capable adapter returns ``last_frame_bytes``.
* **idempotency** — re-submitting the same ``shot_id`` does not double-spend.
* **cancellation** — an in-flight task can be cancelled to a terminal state.
* **timeout** — a task that never completes raises ``ProviderTimeout``.
* **spend gate** — ``LiveVideoDisabled`` is honoured and never miscounted.

The adapter under test must be *driveable against the scripted transport*. The
runner therefore takes a ``rebuild`` factory: ``rebuild(script) -> provider``
rebuilds the adapter bound to a freshly-scripted transport for each check (so a
check that injects faults doesn't poison the next). Adapters whose construction
the harness can't see (real hosted providers) supply a thin fake wrapper for
conformance — exactly the fakes shipped in :mod:`.fakes`. When the rebuilt
provider exposes a ``transport`` attribute, the harness *observes* it to assert
wire-level guarantees (request mapping, idempotent submit, cancellation reaches
the transport); when it does not, those checks fall back to result-only
assertions or skip.

Determinism: the runner never sleeps, never reads a clock for control flow, and
``generated_at`` is injectable, so a report is byte-reproducible in tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.providers.errors import (
    AuthenticationError,
    LiveVideoDisabled,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
    TransientProviderError,
)
from app.providers.types import VideoResult, WanMode, WanSpec

from .protocol import (
    ConformantVideoProvider,
    ProviderSurface,
    SubmittedTask,
    VideoCapabilities,
    provider_surface,
)
from .report import (
    CheckOutcome,
    CheckResult,
    ConformanceCheck,
    ConformanceReport,
)
from .transport import Fault, ScriptedTransport, TransportScript

logger = get_logger("app.video.conformance")

#: ``rebuild(script)`` returns a provider freshly bound to a scripted transport.
ProviderFactory = Callable[[TransportScript], ConformantVideoProvider]


# --------------------------------------------------------------------------- #
# The probe: a provider + how to rebuild and observe it
# --------------------------------------------------------------------------- #


class _Probe:
    """A provider under test plus the means to rebuild + observe it per check.

    Every fault-injecting check calls :meth:`rebuild` to get a *fresh* provider
    bound to a scripted transport, so one check's injected faults never bleed
    into the next. When the rebuilt provider exposes a ``transport`` attribute,
    :attr:`last_transport` lets a check read wire-level call counts and the last
    submit body — otherwise it stays ``None`` and the check degrades gracefully.
    """

    def __init__(
        self,
        provider: ConformantVideoProvider,
        rebuild: ProviderFactory,
        rebuild_gated: ProviderFactory | None = None,
    ) -> None:
        self.provider = provider
        self._rebuild = rebuild
        #: A factory that rebuilds the adapter with the spend gate CLOSED, so the
        #: SPEND_GATE check can verify the closed-gate path (raise + no submit).
        #: When ``None``, the spend-gate check observes only the provided
        #: (gate-open or -closed) provider and cannot prove the closed path.
        self._rebuild_gated = rebuild_gated
        self.surface: ProviderSurface = provider_surface(provider)
        #: The adapter's declared capabilities, or a reason it could not be read.
        #: A broken/missing ``capabilities()`` must FAIL the SURFACE check, not
        #: crash the run — so we capture the error rather than let it propagate.
        self.capabilities: VideoCapabilities | None = None
        self.capabilities_error: str | None = None
        if self.surface.has_capabilities:
            try:
                caps = provider.capabilities()
            except Exception as exc:  # noqa: BLE001 - a defective adapter must not crash the run
                self.capabilities_error = f"{type(exc).__name__}: {exc}"
            else:
                if isinstance(caps, VideoCapabilities):
                    self.capabilities = caps
                else:
                    self.capabilities_error = (
                        f"capabilities() returned {type(caps).__name__}, want VideoCapabilities"
                    )
        self.last_transport: ScriptedTransport | None = self._transport_of(provider)

    @property
    def caps(self) -> VideoCapabilities:
        """The declared capabilities; only call when :attr:`capabilities` is set."""
        assert self.capabilities is not None
        return self.capabilities

    @staticmethod
    def _transport_of(provider: ConformantVideoProvider) -> ScriptedTransport | None:
        transport = getattr(provider, "transport", None)
        return transport if isinstance(transport, ScriptedTransport) else None

    def rebuild(self, script: TransportScript) -> ConformantVideoProvider:
        """A fresh provider bound to ``script``; records its observable transport."""
        provider = self._rebuild(script)
        self.last_transport = self._transport_of(provider)
        return provider

    def rebuild_gated(self, script: TransportScript) -> ConformantVideoProvider | None:
        """A fresh provider with the spend gate CLOSED, or ``None`` if unavailable."""
        if self._rebuild_gated is None:
            return None
        provider = self._rebuild_gated(script)
        self.last_transport = self._transport_of(provider)
        return provider

    @property
    def observable(self) -> bool:
        """True when rebuilt providers expose a transport the harness can inspect."""
        return self._transport_of(self._rebuild(TransportScript.healthy())) is not None


def _spec_for_mode(mode: WanMode, caps: VideoCapabilities, *, duration_s: int) -> WanSpec:
    """A minimally-valid :class:`WanSpec` exercising ``mode``'s required inputs."""
    resolution = caps.example_resolution()
    base: dict[str, object] = {
        "mode": mode,
        "prompt": "a quiet room at dusk",
        "duration_s": duration_s,
        "resolution": resolution,
        "shot_id": f"shot-{mode.value}-{duration_s}",
    }
    if caps.supports_seed:
        base["seed"] = 7
    if caps.supports_negative_prompt:
        base["negative_prompt"] = "blurry, low quality"
    if mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
        base["image_url"] = "https://fake.invalid/in/start.png"
    if mode is WanMode.VIDEO_CONTINUATION:
        base["source_video_url"] = "https://fake.invalid/in/prev.mp4"
    if mode is WanMode.FIRST_LAST_FRAME:
        base["first_frame_url"] = "https://fake.invalid/in/first.png"
        base["last_frame_url"] = "https://fake.invalid/in/last.png"
    if mode is WanMode.REFERENCE_TO_VIDEO:
        base["reference_image_urls"] = ["https://fake.invalid/in/ref.png"]
    if mode is WanMode.INSTRUCTION_EDIT:
        base["source_video_url"] = "https://fake.invalid/in/src.mp4"
    return WanSpec(**base)


# --------------------------------------------------------------------------- #
# Individual checks (each returns a CheckResult)
# --------------------------------------------------------------------------- #


def _check_surface(probe: _Probe) -> CheckResult:
    surface = probe.surface
    missing = [
        name
        for name, present in (
            ("render", surface.has_render),
            ("capabilities", surface.has_capabilities),
        )
        if not present
    ]
    if missing:
        return CheckResult(
            check=ConformanceCheck.SURFACE,
            outcome=CheckOutcome.FAIL,
            detail=f"missing required members: {', '.join(missing)}",
        )
    name = getattr(probe.provider, "name", None)
    if not isinstance(name, str) or not name:
        return CheckResult(
            check=ConformanceCheck.SURFACE,
            outcome=CheckOutcome.FAIL,
            detail="provider.name must be a non-empty str",
        )
    if probe.capabilities is None:
        return CheckResult(
            check=ConformanceCheck.SURFACE,
            outcome=CheckOutcome.FAIL,
            detail=f"capabilities() unusable: {probe.capabilities_error}",
        )
    return CheckResult(
        check=ConformanceCheck.SURFACE,
        outcome=CheckOutcome.PASS,
        detail="render + capabilities + name present",
    )


def _check_capability_declaration(probe: _Probe) -> CheckResult:
    if probe.capabilities is None:
        return CheckResult(
            check=ConformanceCheck.CAPABILITY_DECLARATION,
            outcome=CheckOutcome.FAIL,
            detail=f"capabilities() unusable: {probe.capabilities_error}",
        )
    caps = probe.caps
    problems: list[str] = []
    if not caps.modes:
        problems.append("declares no modes")
    if not caps.resolutions:
        problems.append("declares no resolutions")
    if caps.cancellable and not caps.staged_lifecycle:
        problems.append("claims cancellable without a staged lifecycle")
    if caps.idempotent_submit and not caps.staged_lifecycle:
        problems.append("claims idempotent_submit without a staged lifecycle")
    if caps.staged_lifecycle and not probe.surface.has_staged_lifecycle:
        problems.append("claims staged_lifecycle but submit/poll/fetch missing")
    if caps.cancellable and not probe.surface.has_cancel:
        problems.append("claims cancellable but cancel() is missing")
    if problems:
        return CheckResult(
            check=ConformanceCheck.CAPABILITY_DECLARATION,
            outcome=CheckOutcome.FAIL,
            detail="; ".join(problems),
        )
    return CheckResult(
        check=ConformanceCheck.CAPABILITY_DECLARATION,
        outcome=CheckOutcome.PASS,
        detail=(
            f"{len(caps.modes)} modes, durations {caps.durations.min_s}-"
            f"{caps.durations.max_s}s, {len(caps.resolutions)} resolutions"
        ),
    )


async def _check_capability_honesty(probe: _Probe) -> CheckResult:
    """Every declared claim must render; every undeclared one must be rejected."""
    caps = probe.caps

    # 1. Declared modes must each render against a healthy transport.
    for mode in sorted(caps.modes, key=lambda m: m.value):
        provider = probe.rebuild(TransportScript.healthy())
        spec = _spec_for_mode(mode, caps, duration_s=caps.durations.representative_inside())
        try:
            await provider.render(spec)
        except LiveVideoDisabled:
            return CheckResult(
                check=ConformanceCheck.CAPABILITY_HONESTY,
                outcome=CheckOutcome.SKIP,
                detail="spend gate closed; honesty verified under SPEND_GATE",
                subject=f"mode={mode.value}",
            )
        except ProviderError as exc:
            return CheckResult(
                check=ConformanceCheck.CAPABILITY_HONESTY,
                outcome=CheckOutcome.FAIL,
                detail=f"declared mode failed to render: {exc}",
                subject=f"mode={mode.value}",
            )

    # 2. Undeclared modes must be rejected (non-retryable bad request).
    for mode in WanMode:
        if caps.supports_mode(mode):
            continue
        provider = probe.rebuild(TransportScript.healthy())
        spec = _spec_for_mode(mode, caps, duration_s=caps.durations.representative_inside())
        if not await _is_rejected(provider, spec):
            return CheckResult(
                check=ConformanceCheck.CAPABILITY_HONESTY,
                outcome=CheckOutcome.FAIL,
                detail="rendered an UNDECLARED mode (capability over-claim / under-guard)",
                subject=f"mode={mode.value}",
            )

    # 3. Out-of-window durations must be rejected.
    for duration in filter(None, (caps.durations.just_below(), caps.durations.just_above())):
        provider = probe.rebuild(TransportScript.healthy())
        spec = _spec_for_mode(caps.example_mode(), caps, duration_s=duration)
        if not await _is_rejected(provider, spec):
            return CheckResult(
                check=ConformanceCheck.CAPABILITY_HONESTY,
                outcome=CheckOutcome.FAIL,
                detail="accepted an out-of-window duration it does not declare",
                subject=f"duration_s={duration}",
            )

    # 4. An undeclared resolution must be rejected.
    bogus_res = "1234X"
    if not caps.supports_resolution(bogus_res):
        provider = probe.rebuild(TransportScript.healthy())
        spec = _spec_for_mode(caps.example_mode(), caps, duration_s=caps.durations.min_s)
        spec = spec.model_copy(update={"resolution": bogus_res})
        if not await _is_rejected(provider, spec):
            return CheckResult(
                check=ConformanceCheck.CAPABILITY_HONESTY,
                outcome=CheckOutcome.FAIL,
                detail="accepted an undeclared resolution",
                subject=f"resolution={bogus_res}",
            )

    return CheckResult(
        check=ConformanceCheck.CAPABILITY_HONESTY,
        outcome=CheckOutcome.PASS,
        detail="all declared claims render; all undeclared claims rejected",
    )


async def _is_rejected(provider: ConformantVideoProvider, spec: WanSpec) -> bool:
    """True iff ``render(spec)`` declined the request (did not fabricate a clip).

    A non-retryable :class:`ProviderBadRequest` is the canonical rejection; any
    other provider error still counts (the render did not happen), and a closed
    spend gate is treated as "did not over-claim" since the claim is unprobeable.
    """
    try:
        await provider.render(spec)
    except LiveVideoDisabled:
        return True
    except ProviderError:
        return True
    return False


async def _check_request_mapping(probe: _Probe) -> CheckResult:
    """A canonical spec must round-trip through the native request body + result."""
    caps = probe.caps
    mode = caps.example_mode()
    spec = _spec_for_mode(mode, caps, duration_s=caps.durations.representative_inside())
    provider = probe.rebuild(TransportScript.healthy())
    transport = probe.last_transport
    try:
        result = await provider.render(spec)
    except LiveVideoDisabled:
        return CheckResult(
            check=ConformanceCheck.REQUEST_MAPPING,
            outcome=CheckOutcome.SKIP,
            detail="spend gate closed; mapping unobservable",
        )
    except ProviderError as exc:
        return CheckResult(
            check=ConformanceCheck.REQUEST_MAPPING,
            outcome=CheckOutcome.FAIL,
            detail=f"canonical spec did not render: {exc}",
        )

    if transport is not None and transport.last_submit_body is not None:
        missing = _missing_mapped_fields(spec, transport.last_submit_body, caps)
        if missing:
            return CheckResult(
                check=ConformanceCheck.REQUEST_MAPPING,
                outcome=CheckOutcome.FAIL,
                detail=f"native body dropped canonical fields: {', '.join(sorted(set(missing)))}",
            )

    echo = _result_echoes(spec, result)
    if echo is not None:
        return CheckResult(
            check=ConformanceCheck.REQUEST_MAPPING,
            outcome=CheckOutcome.FAIL,
            detail=echo,
        )
    where = "native body + result" if transport is not None else "result"
    return CheckResult(
        check=ConformanceCheck.REQUEST_MAPPING,
        outcome=CheckOutcome.PASS,
        detail=f"prompt/duration/seed/conditioning round-tripped ({where})",
    )


def _missing_mapped_fields(
    spec: WanSpec, body: dict[str, object], caps: VideoCapabilities
) -> list[str]:
    """Canonical fields that should appear (in some form) in the native body."""
    flat = _flatten_scalars(body)
    missing: list[str] = []
    if spec.prompt and spec.prompt not in flat:
        missing.append("prompt")
    if caps.supports_seed and spec.seed is not None and str(spec.seed) not in flat:
        missing.append("seed")
    if caps.supports_negative_prompt and spec.negative_prompt and spec.negative_prompt not in flat:
        missing.append("negative_prompt")
    for url in (
        spec.image_url,
        spec.first_frame_url,
        spec.last_frame_url,
        spec.source_video_url,
        *spec.reference_image_urls,
    ):
        if url and url not in flat:
            missing.append("conditioning_url")
            break
    return missing


def _flatten_scalars(node: object) -> set[str]:
    """Every scalar (str/number) value anywhere inside a nested dict/list body."""
    out: set[str] = set()
    if isinstance(node, dict):
        for value in node.values():
            out |= _flatten_scalars(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            out |= _flatten_scalars(item)
    elif isinstance(node, str):
        out.add(node)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.add(str(node))
    return out


def _result_echoes(spec: WanSpec, result: VideoResult) -> str | None:
    """``None`` when the result faithfully echoes the request; else a reason."""
    if result.mode is not spec.mode:
        return f"result.mode {result.mode} != requested {spec.mode}"
    if round(result.duration_s) != spec.duration_s:
        return f"result.duration_s {result.duration_s} != requested {spec.duration_s}"
    return None


async def _check_error_taxonomy(probe: _Probe) -> CheckResult:
    """Each wire fault must surface as the matching shared error class."""
    caps = probe.caps
    mode = caps.example_mode()
    expected: dict[Fault, tuple[type[ProviderError], bool]] = {
        Fault.BAD_REQUEST: (ProviderBadRequest, False),
        Fault.AUTH: (AuthenticationError, False),
        Fault.RATE_LIMITED: (RateLimited, True),
        Fault.TRANSIENT: (TransientProviderError, True),
    }

    for fault, (want_cls, want_retryable) in expected.items():
        provider = probe.rebuild(TransportScript.with_submit_faults([fault]))
        spec = _spec_for_mode(mode, caps, duration_s=caps.durations.min_s)
        try:
            await provider.render(spec)
        except LiveVideoDisabled:
            return CheckResult(
                check=ConformanceCheck.ERROR_TAXONOMY,
                outcome=CheckOutcome.SKIP,
                detail="spend gate closed; faults unreachable",
            )
        except ProviderError as exc:
            if not isinstance(exc, want_cls):
                return CheckResult(
                    check=ConformanceCheck.ERROR_TAXONOMY,
                    outcome=CheckOutcome.FAIL,
                    detail=f"surfaced as {type(exc).__name__}, want {want_cls.__name__}",
                    subject=fault.name,
                )
            if exc.retryable is not want_retryable:
                return CheckResult(
                    check=ConformanceCheck.ERROR_TAXONOMY,
                    outcome=CheckOutcome.FAIL,
                    detail=f"retryable={exc.retryable}, want {want_retryable}",
                    subject=fault.name,
                )
        else:
            return CheckResult(
                check=ConformanceCheck.ERROR_TAXONOMY,
                outcome=CheckOutcome.FAIL,
                detail="fault was swallowed — render returned instead of raising",
                subject=fault.name,
            )
    return CheckResult(
        check=ConformanceCheck.ERROR_TAXONOMY,
        outcome=CheckOutcome.PASS,
        detail="4xx/401/429/5xx map to the shared taxonomy with correct retryability",
    )


async def _check_asset_handling(probe: _Probe) -> CheckResult:
    """``render`` must return real clip bytes (eager download, no bare URL)."""
    caps = probe.caps
    provider = probe.rebuild(TransportScript.healthy())
    spec = _spec_for_mode(caps.example_mode(), caps, duration_s=caps.durations.min_s)
    try:
        result = await provider.render(spec)
    except LiveVideoDisabled:
        return CheckResult(
            check=ConformanceCheck.ASSET_HANDLING,
            outcome=CheckOutcome.SKIP,
            detail="spend gate closed; asset bytes unobservable",
        )
    except ProviderError as exc:
        return CheckResult(
            check=ConformanceCheck.ASSET_HANDLING,
            outcome=CheckOutcome.FAIL,
            detail=f"render failed: {exc}",
        )
    if caps.eager_download and not result.clip_bytes:
        return CheckResult(
            check=ConformanceCheck.ASSET_HANDLING,
            outcome=CheckOutcome.FAIL,
            detail="claims eager_download but returned no clip_bytes (URLs expire!)",
        )
    transport = probe.last_transport
    if caps.eager_download and transport is not None and transport.fetch_calls < 1:
        return CheckResult(
            check=ConformanceCheck.ASSET_HANDLING,
            outcome=CheckOutcome.FAIL,
            detail="returned clip_bytes without downloading from the transport",
        )
    return CheckResult(
        check=ConformanceCheck.ASSET_HANDLING,
        outcome=CheckOutcome.PASS,
        detail=f"returned {len(result.clip_bytes or b'')} bytes of clip (eager download)",
    )


async def _check_last_frame(probe: _Probe) -> CheckResult:
    caps = probe.caps
    if not caps.extracts_last_frame:
        return CheckResult(
            check=ConformanceCheck.LAST_FRAME,
            outcome=CheckOutcome.SKIP,
            detail="adapter does not claim last-frame extraction",
        )
    provider = probe.rebuild(TransportScript.healthy())
    spec = _spec_for_mode(caps.example_mode(), caps, duration_s=caps.durations.min_s)
    try:
        result = await provider.render(spec)
    except LiveVideoDisabled:
        return CheckResult(
            check=ConformanceCheck.LAST_FRAME,
            outcome=CheckOutcome.SKIP,
            detail="spend gate closed",
        )
    except ProviderError as exc:
        return CheckResult(
            check=ConformanceCheck.LAST_FRAME,
            outcome=CheckOutcome.FAIL,
            detail=f"render failed: {exc}",
        )
    if not result.last_frame_bytes:
        return CheckResult(
            check=ConformanceCheck.LAST_FRAME,
            outcome=CheckOutcome.FAIL,
            detail="claims last-frame extraction but returned none",
        )
    return CheckResult(
        check=ConformanceCheck.LAST_FRAME,
        outcome=CheckOutcome.PASS,
        detail=f"returned {len(result.last_frame_bytes)} bytes of last frame",
    )


async def _check_idempotency(probe: _Probe) -> CheckResult:
    caps = probe.caps
    if not (caps.staged_lifecycle and caps.idempotent_submit and probe.surface.has_submit):
        return CheckResult(
            check=ConformanceCheck.IDEMPOTENCY,
            outcome=CheckOutcome.SKIP,
            detail="adapter does not claim idempotent staged submit",
        )
    provider = probe.rebuild(TransportScript.healthy())
    transport = probe.last_transport
    spec = _spec_for_mode(caps.example_mode(), caps, duration_s=caps.durations.min_s)
    spec = spec.model_copy(update={"shot_id": "shot-idem-1"})
    try:
        first = await provider.submit(spec)
        second = await provider.submit(spec)
    except LiveVideoDisabled:
        return CheckResult(
            check=ConformanceCheck.IDEMPOTENCY,
            outcome=CheckOutcome.SKIP,
            detail="spend gate closed",
        )
    except ProviderError as exc:
        return CheckResult(
            check=ConformanceCheck.IDEMPOTENCY,
            outcome=CheckOutcome.FAIL,
            detail=f"submit failed: {exc}",
        )
    if not isinstance(first, SubmittedTask) or not isinstance(second, SubmittedTask):
        return CheckResult(
            check=ConformanceCheck.IDEMPOTENCY,
            outcome=CheckOutcome.FAIL,
            detail="submit must return a SubmittedTask handle",
        )
    if first.task_id != second.task_id:
        return CheckResult(
            check=ConformanceCheck.IDEMPOTENCY,
            outcome=CheckOutcome.FAIL,
            detail="re-submit produced a different task — would double-spend",
        )
    if transport is not None and transport.submit_calls != 2:
        # Two adapter submits should reach the transport twice, but it must mint
        # only ONE task (asserted above) — i.e. the transport dedupes by shot_id.
        return CheckResult(
            check=ConformanceCheck.IDEMPOTENCY,
            outcome=CheckOutcome.FAIL,
            detail=f"expected 2 transport submits, saw {transport.submit_calls}",
        )
    return CheckResult(
        check=ConformanceCheck.IDEMPOTENCY,
        outcome=CheckOutcome.PASS,
        detail="identical shot_id returns the same task (no double-spend)",
    )


async def _check_cancellation(probe: _Probe) -> CheckResult:
    caps = probe.caps
    if not (caps.cancellable and probe.surface.has_cancel and probe.surface.has_submit):
        return CheckResult(
            check=ConformanceCheck.CANCELLATION,
            outcome=CheckOutcome.SKIP,
            detail="adapter does not claim cancellation",
        )
    # A task that takes several polls so there's an in-flight window to cancel.
    provider = probe.rebuild(TransportScript(ticks_to_done=5))
    transport = probe.last_transport
    spec = _spec_for_mode(caps.example_mode(), caps, duration_s=caps.durations.min_s)
    try:
        task = await provider.submit(spec)
        await provider.cancel(task)
        status = await provider.poll(task)
    except LiveVideoDisabled:
        return CheckResult(
            check=ConformanceCheck.CANCELLATION,
            outcome=CheckOutcome.SKIP,
            detail="spend gate closed",
        )
    except ProviderError as exc:
        return CheckResult(
            check=ConformanceCheck.CANCELLATION,
            outcome=CheckOutcome.FAIL,
            detail=f"cancel/poll failed: {exc}",
        )
    if transport is not None and transport.cancel_calls < 1:
        return CheckResult(
            check=ConformanceCheck.CANCELLATION,
            outcome=CheckOutcome.FAIL,
            detail="cancel() issued no cancel to the transport",
        )
    if getattr(status, "state", None) != "canceled":
        return CheckResult(
            check=ConformanceCheck.CANCELLATION,
            outcome=CheckOutcome.FAIL,
            detail=f"post-cancel poll reports {getattr(status, 'state', '?')!r}, want 'canceled'",
        )
    return CheckResult(
        check=ConformanceCheck.CANCELLATION,
        outcome=CheckOutcome.PASS,
        detail="cancel reaches the transport and the task lands canceled",
    )


async def _check_timeout(probe: _Probe) -> CheckResult:
    """A task that never completes must raise ``ProviderTimeout`` (no hang)."""
    caps = probe.caps
    if not caps.staged_lifecycle:
        return CheckResult(
            check=ConformanceCheck.TIMEOUT,
            outcome=CheckOutcome.SKIP,
            detail="no staged lifecycle to bound a poll deadline",
        )
    provider = probe.rebuild(TransportScript.never_completes())
    spec = _spec_for_mode(caps.example_mode(), caps, duration_s=caps.durations.min_s)
    try:
        await provider.render(spec)
    except LiveVideoDisabled:
        return CheckResult(
            check=ConformanceCheck.TIMEOUT,
            outcome=CheckOutcome.SKIP,
            detail="spend gate closed",
        )
    except ProviderTimeout:
        return CheckResult(
            check=ConformanceCheck.TIMEOUT,
            outcome=CheckOutcome.PASS,
            detail="never-completing task raised ProviderTimeout",
        )
    except ProviderError as exc:
        return CheckResult(
            check=ConformanceCheck.TIMEOUT,
            outcome=CheckOutcome.FAIL,
            detail=f"raised {type(exc).__name__}, want ProviderTimeout",
        )
    return CheckResult(
        check=ConformanceCheck.TIMEOUT,
        outcome=CheckOutcome.FAIL,
        detail="never-completing task returned a result (fabricated?)",
    )


async def _check_spend_gate(probe: _Probe) -> CheckResult:
    """A closed spend gate raises ``LiveVideoDisabled`` and submits nothing.

    With ``KINORA_LIVE_VIDEO`` off (the repo default), ``render`` must raise the
    deliberate gate error *before* any task is submitted — no spend leak. When
    the gate is open the adapter renders normally; the check passes either way as
    long as the closed-gate path is clean.
    """
    caps = probe.caps
    spec = _spec_for_mode(caps.example_mode(), caps, duration_s=caps.durations.min_s)

    # Prefer the gate-CLOSED rebuild so we can verify the deliberate gate path.
    gated = probe.rebuild_gated(TransportScript.healthy())
    if gated is not None:
        transport = probe.last_transport
        try:
            await gated.render(spec)
        except LiveVideoDisabled:
            submitted = transport.submit_calls if transport is not None else 0
            if submitted:
                return CheckResult(
                    check=ConformanceCheck.SPEND_GATE,
                    outcome=CheckOutcome.FAIL,
                    detail="LiveVideoDisabled raised but a task was submitted (spend leak)",
                )
            return CheckResult(
                check=ConformanceCheck.SPEND_GATE,
                outcome=CheckOutcome.PASS,
                detail="gate closed: raised LiveVideoDisabled, submitted nothing",
            )
        except ProviderError as exc:
            return CheckResult(
                check=ConformanceCheck.SPEND_GATE,
                outcome=CheckOutcome.FAIL,
                detail=(
                    f"gate closed but render raised {type(exc).__name__}, "
                    "want LiveVideoDisabled"
                ),
            )
        return CheckResult(
            check=ConformanceCheck.SPEND_GATE,
            outcome=CheckOutcome.FAIL,
            detail="gate closed but render returned a clip (spend leak)",
        )

    # No gate-closed rebuild available — observe whatever the default provider
    # does. A closed gate must raise + submit nothing; an open gate just renders.
    provider = probe.rebuild(TransportScript.healthy())
    transport = probe.last_transport
    try:
        await provider.render(spec)
    except LiveVideoDisabled:
        submitted = transport.submit_calls if transport is not None else 0
        if submitted:
            return CheckResult(
                check=ConformanceCheck.SPEND_GATE,
                outcome=CheckOutcome.FAIL,
                detail="LiveVideoDisabled raised but a task was submitted (spend leak)",
            )
        return CheckResult(
            check=ConformanceCheck.SPEND_GATE,
            outcome=CheckOutcome.PASS,
            detail="gate closed: raised LiveVideoDisabled, submitted nothing",
        )
    except ProviderError as exc:
        return CheckResult(
            check=ConformanceCheck.SPEND_GATE,
            outcome=CheckOutcome.FAIL,
            detail=f"render raised {type(exc).__name__}; gate handling unverifiable",
        )
    return CheckResult(
        check=ConformanceCheck.SPEND_GATE,
        outcome=CheckOutcome.SKIP,
        detail="gate open and no gate-closed rebuild supplied; closed path unverified",
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

_SYNC_CHECKS: tuple[tuple[ConformanceCheck, Callable[[_Probe], CheckResult]], ...] = (
    (ConformanceCheck.SURFACE, _check_surface),
    (ConformanceCheck.CAPABILITY_DECLARATION, _check_capability_declaration),
)
_ASYNC_CHECKS: tuple[tuple[ConformanceCheck, Callable[[_Probe], Awaitable[CheckResult]]], ...] = (
    (ConformanceCheck.CAPABILITY_HONESTY, _check_capability_honesty),
    (ConformanceCheck.REQUEST_MAPPING, _check_request_mapping),
    (ConformanceCheck.ERROR_TAXONOMY, _check_error_taxonomy),
    (ConformanceCheck.ASSET_HANDLING, _check_asset_handling),
    (ConformanceCheck.LAST_FRAME, _check_last_frame),
    (ConformanceCheck.IDEMPOTENCY, _check_idempotency),
    (ConformanceCheck.CANCELLATION, _check_cancellation),
    (ConformanceCheck.TIMEOUT, _check_timeout),
    (ConformanceCheck.SPEND_GATE, _check_spend_gate),
)


async def run_conformance(
    provider: ConformantVideoProvider,
    *,
    rebuild: ProviderFactory | None = None,
    rebuild_gated: ProviderFactory | None = None,
    now: datetime | None = None,
) -> ConformanceReport:
    """Run every conformance check against ``provider`` and score the result.

    Args:
        provider: The adapter under test. Must satisfy
            :class:`~app.video.conformance.protocol.ConformantVideoProvider`.
        rebuild: ``rebuild(script) -> provider`` rebuilds the adapter bound to a
            freshly-scripted transport for each fault-injecting check. When
            omitted the same ``provider`` is reused for every check — fine for a
            stateless ``render``, but adapters that need per-check fault scripts
            (every shipped fake) supply one.
        rebuild_gated: ``rebuild(script) -> provider`` but with the spend gate
            CLOSED, so the SPEND_GATE check can verify the deliberate
            ``LiveVideoDisabled`` path raises *and submits nothing*. Omit it only
            when the adapter cannot be constructed gate-closed; the spend-gate
            check then degrades to SKIP (or observes a closed default provider).
        now: Inject the report timestamp for deterministic tests.

    Returns:
        A :class:`ConformanceReport`. The runner never raises on an adapter
        defect — defects are recorded as FAIL results; even a *harness* bug is
        captured per-check as ERROR rather than propagating.
    """
    factory = rebuild or (lambda _script: provider)
    probe = _Probe(provider, factory, rebuild_gated)
    results: list[CheckResult] = []

    for check, fn in _SYNC_CHECKS:
        results.append(_safe_sync(check, fn, probe))

    surface_result = results[0]
    if surface_result.outcome is CheckOutcome.FAIL:
        # No usable surface — the async checks would all ERROR; record them SKIP.
        for check, _fn in _ASYNC_CHECKS:
            results.append(
                CheckResult(
                    check=check,
                    outcome=CheckOutcome.SKIP,
                    detail="skipped: required surface missing",
                )
            )
    else:
        for check, afn in _ASYNC_CHECKS:
            results.append(await _safe_async(check, afn, probe))

    report = ConformanceReport(
        provider_id=getattr(provider, "name", "unknown"),
        results=results,
        generated_at=now or datetime.now(UTC),
    )
    logger.info(
        "video.conformance.report",
        provider=report.provider_id,
        passed=report.passed,
        score=round(report.score, 3),
        failing=[r.check.value for r in report.failures],
    )
    return report


def _safe_sync(
    check: ConformanceCheck, fn: Callable[[_Probe], CheckResult], probe: _Probe
) -> CheckResult:
    try:
        return fn(probe)
    except Exception as exc:  # noqa: BLE001 - a harness bug must not crash the run
        return CheckResult(
            check=check,
            outcome=CheckOutcome.ERROR,
            detail=f"harness error: {type(exc).__name__}: {exc}",
        )


async def _safe_async(
    check: ConformanceCheck, fn: Callable[[_Probe], Awaitable[CheckResult]], probe: _Probe
) -> CheckResult:
    try:
        return await fn(probe)
    except Exception as exc:  # noqa: BLE001 - a harness bug must not crash the run
        return CheckResult(
            check=check,
            outcome=CheckOutcome.ERROR,
            detail=f"harness error: {type(exc).__name__}: {exc}",
        )


__all__ = [
    "ProviderFactory",
    "run_conformance",
]
