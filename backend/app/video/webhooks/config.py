"""Build the gateway's signing registry + default sink from settings.

Provider signing secrets are optional configuration: a deployment registers only
the providers it actually receives callbacks from. With *none* configured the
route still mounts and answers — every unknown provider is a clean 404 — so the
subsystem is safe to ship dark and light up per-provider as secrets land.

The default sink is a structured-logging no-op (:class:`LoggingJobCompletionSink`)
so the whole path runs end-to-end with no job engine merged. The orchestrator
replaces it with the real :class:`~app.video.webhooks.models.JobCompletionSink`
when the async job lifecycle is wired — a one-line swap, no route change.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.video.webhooks.models import ProviderCallback
from app.video.webhooks.signing import ProviderSigningConfig, SignatureVerifier

logger = get_logger("app.video.webhooks.config")


class LoggingJobCompletionSink:
    """A safe default sink: log the callback, do no work, never raise.

    Satisfies the :class:`JobCompletionSink` Protocol structurally. Lets the
    ingress run fully (verify → parse → dedup → handoff) before any real job
    engine exists, which is exactly what the FINAL-round constraint requires.
    """

    async def on_callback(self, callback: ProviderCallback) -> None:
        logger.info(
            "video.webhook.sink.noop",
            provider=callback.provider,
            task_id=callback.provider_task_id,
            status=callback.status.value,
            terminal=callback.status.is_terminal,
        )


def build_verifier(settings: object) -> SignatureVerifier:
    """Assemble a :class:`SignatureVerifier` from the configured provider secrets.

    Reads additive, optional ``video_webhook_*`` settings (see
    ``app.core.config``). Each provider is registered only when its secret is set.
    Accepts a duck-typed ``settings`` so this module needn't import the concrete
    Settings type (keeps it unit-testable with a stub).
    """
    tolerance = int(getattr(settings, "video_webhook_tolerance_s", 300))
    verifier = SignatureVerifier()
    secret_attr = {
        "wan": "video_webhook_wan_secret",
        "dashscope": "video_webhook_dashscope_secret",
        "minimax": "video_webhook_minimax_secret",
        "kinora": "video_webhook_internal_secret",
    }
    for provider, attr in secret_attr.items():
        secret = getattr(settings, attr, None)
        if secret:
            verifier.register(
                ProviderSigningConfig(
                    provider=provider,
                    secret=str(secret),
                    tolerance_s=tolerance,
                )
            )
    return verifier


__all__ = ["LoggingJobCompletionSink", "build_verifier"]
