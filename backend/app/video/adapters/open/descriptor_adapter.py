"""``DescriptorAdapter`` — run *any* video model from a :class:`ProviderDescriptor`.

This is where the descriptor format becomes executable: a single concrete
:class:`~.base.BaseOpenAdapter` subclass that is parameterised *entirely* by a
loaded :class:`~.descriptor.ProviderDescriptor`. Adding a brand-new model is then
purely a config change — drop a descriptor file in and call
:func:`build_from_descriptor`; no new Python.

The adapter implements each abstract hook of the base lifecycle by reading the
descriptor:

* ``capabilities`` ← the descriptor's capability profile;
* ``_build_submit_body`` ← :func:`render_template` over the body template + a
  context flattened from the :class:`WanSpec`;
* ``_submit_path`` / ``_poll_path`` ← path strings with ``{{model}}`` /
  ``{{task_id}}`` interpolated;
* ``_parse_submission`` / ``_parse_status`` ← :mod:`.jsonpath` selectors that pull
  the task id, status, video url (or inline base64), progress and message out of
  whatever shape the provider returns.

The ``ComfyUIProvider`` and ``OpenAPIProvider`` named in the task are *aliases* for
this one engine driven by a ComfyUI- / OpenAPI-shaped descriptor (see the bundled
``descriptors/`` files): the whole point is that one engine drives all of them.
"""

from __future__ import annotations

from typing import Any

from app.providers.types import WanMode, WanSpec

from .base import BaseOpenAdapter, PollConfig
from .descriptor import ProviderDescriptor, _decode_inline_b64, load_descriptor
from .interface import Capabilities, SubmittedTask, TaskStatus
from .jsonpath import select
from .template import build_context, render_template
from .transport import OpenHttpTransport, OpenTransportConfig

__all__ = [
    "ComfyUIProvider",
    "DescriptorAdapter",
    "OpenAPIProvider",
    "build_from_descriptor",
]


class DescriptorAdapter(BaseOpenAdapter):
    """A fully descriptor-driven open-model adapter (config-only onboarding)."""

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        transport: OpenHttpTransport,
        *,
        live_video: bool,
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._capabilities = descriptor.to_capabilities()
        super().__init__(
            transport,
            live_video=live_video,
            poll=poll,
            name=descriptor.name,
            usage_sink=usage_sink,
        )

    @property
    def provider_id(self) -> str:
        return self._descriptor.name

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def capabilities(self) -> Capabilities:
        return self._capabilities

    def native_model(self, spec: WanSpec) -> str:
        return spec.model or self._descriptor.model

    # -- context -------------------------------------------------------- #

    def _context(self, spec: WanSpec) -> dict[str, Any]:
        return build_context(spec, extra={"model": self.native_model(spec)})

    # -- submit --------------------------------------------------------- #

    def _build_submit_body(self, spec: WanSpec) -> dict[str, Any]:
        body = render_template(self._descriptor.submit.body_template, self._context(spec))
        return body if isinstance(body, dict) else {"input": body}

    def _submit_path(self, spec: WanSpec) -> str:
        rendered = render_template(self._descriptor.submit.path, self._context(spec))
        return str(rendered)

    def _parse_submission(self, body: dict[str, Any], model: str) -> SubmittedTask:
        sub = self._descriptor.submit
        task_id = select(body, sub.task_id_path)
        if task_id is None:
            from app.providers.errors import ProviderError

            raise ProviderError(
                f"{self.provider_id} submission returned no task id (path {sub.task_id_path!r})",
            )
        poll_url = select(body, sub.poll_url_path) if sub.poll_url_path else None
        return SubmittedTask(
            task_id=str(task_id),
            model=model,
            poll_url=str(poll_url) if poll_url else None,
            raw=body,
        )

    # -- poll ----------------------------------------------------------- #

    def _poll_path(self, task: SubmittedTask) -> str:
        if task.poll_url:
            return task.poll_url
        rendered = render_template(
            self._descriptor.poll.path,
            {"task_id": task.task_id, "model": task.model},
        )
        return str(rendered)

    def _parse_status(self, body: dict[str, Any], task: SubmittedTask) -> TaskStatus:
        poll = self._descriptor.poll
        # Poll selectors may reference ``{{task_id}}`` / ``{{model}}`` — e.g. a
        # ComfyUI history doc keyed by the prompt id. Render them against the task
        # context so a selector can address a dynamic, id-named result node.
        sel_ctx = {"task_id": task.task_id, "model": task.model}

        def sel(path: str | None) -> Any:
            if not path:
                return None
            return select(body, str(render_template(path, sel_ctx)))

        raw_status = sel(poll.status_path)
        state = self._normalize_state(raw_status)
        message = sel(poll.message_path)
        progress = self._normalize_progress(sel(poll.progress_path))
        video_url: str | None = None
        inline: bytes | None = None
        if state == TaskStatus.SUCCEEDED:
            url_val = sel(poll.video_url_path)
            video_url = str(url_val) if isinstance(url_val, str) and url_val else None
            if poll.inline_b64_path:
                inline = _decode_inline_b64(sel(poll.inline_b64_path))
        return TaskStatus(
            state=state,
            video_url=video_url,
            inline_bytes=inline,
            message=str(message) if message is not None else None,
            progress=progress,
            raw=body,
        )

    def _normalize_state(self, raw_status: Any) -> str:
        poll = self._descriptor.poll
        token = str(raw_status or "").strip().lower()
        if token in {v.lower() for v in poll.succeeded_values}:
            return TaskStatus.SUCCEEDED
        if token in {v.lower() for v in poll.failed_values}:
            return TaskStatus.FAILED
        return TaskStatus.PENDING

    @staticmethod
    def _normalize_progress(value: Any) -> float | None:
        if value is None:
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return num / 100.0 if num > 1.0 else num

    # -- factory -------------------------------------------------------- #

    @classmethod
    def from_descriptor(
        cls,
        descriptor: ProviderDescriptor,
        *,
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        transport: object | None = None,
        settings: Any | None = None,
    ) -> DescriptorAdapter:
        """Build a live adapter from a descriptor + runtime gates.

        ``allow_network`` (default OFF) and ``live_video`` (KINORA_LIVE_VIDEO) are
        supplied here, never baked into the file. ``transport`` is an optional
        injected httpx ``MockTransport`` for tests; ``settings`` lets a test inject
        a :class:`~app.core.config.Settings` so the inner client needs no env.
        """
        cfg = OpenTransportConfig(
            base_url=descriptor.transport.base_url,
            api_key=api_key,
            auth_scheme=descriptor.transport.auth_scheme,
            allow_network=allow_network,
            extra_headers=dict(descriptor.transport.headers),
            timeout_s=descriptor.transport.timeout_s,
        )
        http = OpenHttpTransport(cfg, transport=transport, settings=settings)  # type: ignore[arg-type]
        return cls(
            descriptor,
            http,
            live_video=live_video,
            poll=poll,
            usage_sink=usage_sink,
        )


def build_from_descriptor(
    source: str | ProviderDescriptor | dict[str, Any],
    *,
    api_key: str | None = None,
    allow_network: bool = False,
    live_video: bool = False,
    poll: PollConfig | None = None,
    usage_sink: Any | None = None,
    transport: object | None = None,
    settings: Any | None = None,
) -> DescriptorAdapter:
    """Load a descriptor (path / string / dict / model) and build its adapter.

    The one-call config-only onboarding entry point: point it at a descriptor file
    and you have a router-ready video backend, no code.
    """
    descriptor = source if isinstance(source, ProviderDescriptor) else load_descriptor(source)
    return DescriptorAdapter.from_descriptor(
        descriptor,
        api_key=api_key,
        allow_network=allow_network,
        live_video=live_video,
        poll=poll,
        usage_sink=usage_sink,
        transport=transport,
        settings=settings,
    )


#: ``ComfyUIProvider`` and ``OpenAPIProvider`` are the descriptor engine under the
#: names the task calls out: a self-hosted ComfyUI box or any OpenAPI endpoint is
#: just a descriptor (see ``descriptors/comfyui_example.yaml`` /
#: ``descriptors/openapi_example.yaml``). Keeping them as aliases makes the
#: "one engine, many models" design explicit while honouring the named contract.
ComfyUIProvider = DescriptorAdapter
OpenAPIProvider = DescriptorAdapter


# Mode re-export so descriptor authors importing this module have the enum handy.
_ALL_MODES = tuple(WanMode)
