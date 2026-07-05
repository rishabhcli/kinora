"""create_providers() assembles a cross-provider VideoRouter when more than
one video backend is configured; falls back to a single backend otherwise
(today's unchanged behavior).

Every Settings() below pins BOTH modelscope_api_key and minimax_api_key
explicitly (even to None), rather than relying on field defaults for the ones
a given test wants absent. backend/.env is a real, gitignored local-dev file
that (independent of this task) already sets a real MINIMAX_API_KEY and
VIDEO_BACKEND=minimax for unrelated work; pydantic-settings' env_file=".env"
means a "bare" Settings(dashscope_api_key=...) call would silently inherit
those ambient values and make these tests pass/fail for the wrong reason
depending on the developer's local .env. conftest.py neutralizes a few
specific keys (DASHSCOPE_API_KEY, APP_ENV, REASONING_PROVIDER) the same way
for the same reason, but not minimax_api_key/video_backend, so we do it here.
"""

from __future__ import annotations

from app.core.config import Settings
from app.providers import create_providers
from app.providers.minimax import MiniMaxVideoProvider
from app.providers.modelscope import ModelScopeVideoProvider
from app.providers.video import VideoProvider
from app.providers.video_router import VideoRouter


def test_single_backend_when_only_dashscope_configured() -> None:
    providers = create_providers(
        Settings(
            _env_file=None,
            dashscope_api_key="test",
            modelscope_api_key=None,
            minimax_api_key=None,
        )
    )
    assert isinstance(providers.video, VideoProvider)


def test_router_assembled_when_modelscope_and_minimax_both_configured() -> None:
    providers = create_providers(
        Settings(
            _env_file=None,
            dashscope_api_key="test",
            modelscope_api_key="ms-key",
            minimax_api_key="mm-key",
        )
    )
    assert isinstance(providers.video, VideoRouter)
    # NOTE (confirmed 2026-07-04 by reading video_router.py): VideoRouter has no
    # public `.backends` attribute — backends are stored privately as `_backends`.
    # `available_backends()` is the public accessor (returns backends whose
    # circuit breaker currently permits a call — equivalent to the raw list here
    # since nothing has failed yet in this test).
    backend_names = {b.name for b in providers.video.available_backends()}
    assert any("modelscope" in n for n in backend_names)
    assert any("minimax" in n for n in backend_names)


def test_router_orders_modelscope_before_minimax() -> None:
    providers = create_providers(
        Settings(
            _env_file=None,
            dashscope_api_key="test",
            modelscope_api_key="ms-key",
            minimax_api_key="mm-key",
        )
    )
    assert isinstance(providers.video, VideoRouter)
    backends = providers.video.available_backends()
    ms_idx = next(i for i, b in enumerate(backends) if "modelscope" in b.name)
    mm_idx = next(i for i, b in enumerate(backends) if "minimax" in b.name)
    assert ms_idx < mm_idx, "free ModelScope must be tried before paid MiniMax"


def test_single_backend_when_only_minimax_configured() -> None:
    # No-regression guard: only one non-dashscope backend configured must still
    # produce that backend directly (not wrapped in a VideoRouter), exactly like
    # the pre-campaign single-backend behavior.
    providers = create_providers(
        Settings(
            _env_file=None,
            dashscope_api_key="test",
            modelscope_api_key=None,
            minimax_api_key="mm-key",
        )
    )
    assert isinstance(providers.video, MiniMaxVideoProvider)


def test_single_backend_when_only_modelscope_configured() -> None:
    providers = create_providers(
        Settings(
            _env_file=None,
            dashscope_api_key="test",
            modelscope_api_key="ms-key",
            minimax_api_key=None,
        )
    )
    assert isinstance(providers.video, ModelScopeVideoProvider)
