"""Observability: the structlog processor redacts secrets without breaking output."""

from __future__ import annotations

import json

from app.core.logging import REDACTED, _build_processors, redact_secrets


def _apply(event: dict[str, object]) -> dict[str, object]:
    return redact_secrets(None, "info", event)  # type: ignore[arg-type, return-value]


def test_sensitive_keys_are_redacted() -> None:
    out = _apply(
        {
            "event": "login",
            "authorization": "Bearer eyJhbGci.payload.sig",
            "api_key": "sk-abc123def456",
            "dashscope_api_key": "sk-secretkey0099",
            "password": "hunter2",
            "access_token": "tok_live_998877",
            "secret": "shhh",
        }
    )
    sensitive = (
        "authorization",
        "api_key",
        "dashscope_api_key",
        "password",
        "access_token",
        "secret",
    )
    for key in sensitive:
        assert out[key] == REDACTED, key


def test_operational_token_keys_are_not_over_redacted() -> None:
    # Bare "token" is sensitive, but cancellation/trajectory/task ids are not.
    out = _apply(
        {
            "event": "queue.cancel",
            "token": "supersecret",
            "cancel_token": "traj_abc",
            "trajectory_token": "traj_def",
            "provider_task_id": "task-123",
        }
    )
    assert out["token"] == REDACTED
    assert out["cancel_token"] == "traj_abc"
    assert out["trajectory_token"] == "traj_def"
    assert out["provider_task_id"] == "task-123"


def test_bearer_and_sk_keys_masked_in_message_strings() -> None:
    out = _apply({"event": "calling api with Authorization: Bearer eyJabc.def and key sk-ZZZ12345"})
    message = str(out["event"])
    assert "eyJabc.def" not in message
    assert "sk-ZZZ12345" not in message
    assert REDACTED in message


def test_nested_payloads_are_redacted_recursively() -> None:
    out = _apply(
        {
            "event": "provider.call",
            "headers": {"Authorization": "Bearer abc", "X-Trace": "ok"},
            "items": [{"password": "p"}, {"safe": "v"}],
        }
    )
    headers = out["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == REDACTED
    assert headers["X-Trace"] == "ok"
    items = out["items"]
    assert isinstance(items, list)
    assert items[0] == {"password": REDACTED}
    assert items[1] == {"safe": "v"}


def test_full_processor_chain_renders_json_with_secret_masked() -> None:
    # Run the real chain (redaction precedes the JSON renderer) end-to-end.
    procs = _build_processors(json_logs=True)
    event: object = {
        "event": "startup with key sk-LIVEKEY123456",
        "api_key": "sk-LIVEKEY123456",
    }
    for proc in procs:
        event = proc(None, "info", event)  # type: ignore[arg-type, assignment]
    assert isinstance(event, str)  # JSONRenderer terminates the chain
    rendered = json.loads(event)
    assert rendered["api_key"] == REDACTED
    assert "sk-LIVEKEY123456" not in event
