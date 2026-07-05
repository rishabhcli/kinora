#!/usr/bin/env python3
"""Safe DashScope/Qwen Cloud provider preflight for Kinora deployments.

Default mode checks configuration and hosted Wan model/protocol profiles without
submitting render tasks. Pass ``--probe-video-submit`` when intentionally
collecting model-availability evidence. Pass ``--spend-smoke`` only when
intentionally collecting final demo evidence: it performs tiny chat/VL/image/TTS
calls and may consume credits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.providers import create_providers
from app.providers.errors import ProviderError

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f"
    b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(slots=True)
class Check:
    """One preflight result."""

    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _safe_base(settings: Settings) -> str:
    """Return the configured provider base URL; contains no secret material."""
    return settings.dashscope_base_url.rstrip("/")


async def _check_video_model(
    providers: Any,
    model: str,
    name: str,
    *,
    submit_probe: bool,
) -> Check:
    """Check one video model/role against whatever backend is actually wired.

    Since Tasks 2/3, ``providers.video`` may be a single non-Wan backend
    (``MiniMaxVideoProvider``, ``ModelScopeVideoProvider``) or a
    ``VideoRouter`` wrapping several — none of which have Wan's
    ``profile_for_model``/``verify_model_available``. Every backend (and the
    router itself) is guaranteed only the generic ``VideoBackend`` contract
    (``name`` + ``healthy()``), so that's the fallback when the richer,
    Wan-specific detail isn't available.
    """
    backend = providers.video
    profile_detail = ""
    if hasattr(backend, "profile_for_model"):
        profile = backend.profile_for_model(model)
        profile_detail = f" protocol={profile.protocol.value}"
    if not submit_probe:
        return Check(
            name,
            True,
            f"backend={backend.name} model={model}{profile_detail} submit_probe=skipped",
        )
    try:
        if hasattr(backend, "verify_model_available"):
            ok = await backend.verify_model_available(model)
        else:
            ok = await backend.healthy()
    except ProviderError as exc:
        return Check(name, False, f"{type(exc).__name__}: {exc}")
    return Check(
        name,
        ok,
        f"backend={backend.name} model={model}{profile_detail}"
        if ok
        else f"backend={backend.name} unavailable: {model}",
    )


async def _spend_smoke(settings: Settings, providers: Any) -> list[Check]:
    checks: list[Check] = []
    try:
        result = await providers.chat.chat(
            [{"role": "user", "content": "Reply with exactly: ok"}],
            settings.chat_model_adapter,
            max_tokens=8,
            stream=False,
            enable_thinking=False,
        )
        checks.append(Check("chat_smoke", bool(result.text.strip()), settings.chat_model_adapter))
    except ProviderError as exc:
        checks.append(Check("chat_smoke", False, f"{type(exc).__name__}: {exc}"))

    try:
        text = await providers.vl.analyze(
            [_TINY_PNG],
            "Reply with exactly: ok",
            model=settings.vl_model,
            max_tokens=8,
        )
        checks.append(Check("vl_smoke", bool(text.strip()), settings.vl_model))
    except ProviderError as exc:
        checks.append(Check("vl_smoke", False, f"{type(exc).__name__}: {exc}"))

    try:
        images = await providers.image.generate(
            "A tiny gold book icon on a plain dark background",
            size="1328*1328",  # qwen-image-plus's allowed sizes exclude 512*512
            n=1,
            model=settings.image_model,
        )
        checks.append(Check("image_smoke", bool(images and images[0]), settings.image_model))
    except ProviderError as exc:
        checks.append(Check("image_smoke", False, f"{type(exc).__name__}: {exc}"))

    try:
        tts = await providers.tts.synthesize("Kinora preflight.", voice_id="Cherry")
        checks.append(Check("tts_smoke", bool(tts.audio_bytes), tts.model))
    except ProviderError as exc:
        checks.append(Check("tts_smoke", False, f"{type(exc).__name__}: {exc}"))
    return checks


async def run_preflight(*, spend_smoke: bool, probe_video_submit: bool) -> list[Check]:
    """Run configured provider checks and return structured results."""
    settings = get_settings()
    providers = create_providers(settings)
    checks = [
        Check("dashscope_base", True, _safe_base(settings)),
        Check("chat_configured", True, settings.chat_model_adapter),
        Check("vl_configured", True, settings.vl_model),
        Check("image_configured", True, settings.image_model),
        Check("tts_configured", True, settings.tts_model),
    ]
    try:
        checks.extend(
            [
                await _check_video_model(
                    providers,
                    settings.video_model,
                    "video_t2v",
                    submit_probe=probe_video_submit,
                ),
                await _check_video_model(
                    providers,
                    settings.video_model_i2v,
                    "video_i2v",
                    submit_probe=probe_video_submit,
                ),
                await _check_video_model(
                    providers,
                    settings.video_model_r2v,
                    "video_r2v",
                    submit_probe=probe_video_submit,
                ),
            ]
        )
        if spend_smoke:
            checks.extend(await _spend_smoke(settings, providers))
    finally:
        await providers.aclose()
    return checks


def _print_text(checks: list[Check]) -> None:
    for check in checks:
        status = "ok" if check.ok else "fail"
        print(f"{status:4} {check.name}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument(
        "--probe-video-submit",
        action="store_true",
        help="submit empty hosted-Wan availability probes; may create non-rendering tasks",
    )
    parser.add_argument(
        "--spend-smoke",
        action="store_true",
        help="run tiny live chat/VL/image/TTS calls; may consume DashScope credits",
    )
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    checks = asyncio.run(
        run_preflight(
            spend_smoke=bool(args.spend_smoke),
            probe_video_submit=bool(args.probe_video_submit),
        )
    )
    if args.json:
        print(json.dumps([check.as_dict() for check in checks], indent=2))
    else:
        _print_text(checks)
    return 0 if all(check.ok for check in checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
