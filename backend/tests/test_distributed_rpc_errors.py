"""Tests for the RPC error taxonomy + status classification."""

from __future__ import annotations

from app.distributed.rpc.errors import (
    FailureKind,
    RpcError,
    RpcStatus,
    cancelled,
    deadline_exceeded,
    internal,
    invalid_argument,
    not_found,
    resource_exhausted,
    unavailable,
)


def test_retryable_statuses() -> None:
    assert RpcStatus.UNAVAILABLE.retryable
    assert RpcStatus.DEADLINE_EXCEEDED.retryable
    assert RpcStatus.RESOURCE_EXHAUSTED.retryable
    assert RpcStatus.ABORTED.retryable
    assert RpcStatus.CANCELLED.retryable


def test_non_retryable_statuses() -> None:
    assert not RpcStatus.INVALID_ARGUMENT.retryable
    assert not RpcStatus.NOT_FOUND.retryable
    assert not RpcStatus.PERMISSION_DENIED.retryable
    assert not RpcStatus.UNIMPLEMENTED.retryable
    assert not RpcStatus.OK.retryable


def test_client_error_classification() -> None:
    assert RpcStatus.INVALID_ARGUMENT.is_client_error
    assert RpcStatus.UNAUTHENTICATED.is_client_error
    assert not RpcStatus.INTERNAL.is_client_error
    assert not RpcStatus.UNAVAILABLE.is_client_error


def test_http_roundtrip() -> None:
    for status in RpcStatus:
        http = status.to_http()
        assert 100 <= http < 600
    # The canonical mappings round-trip back.
    assert RpcStatus.from_http(404) is RpcStatus.NOT_FOUND
    assert RpcStatus.from_http(429) is RpcStatus.RESOURCE_EXHAUSTED
    assert RpcStatus.from_http(504) is RpcStatus.DEADLINE_EXCEEDED
    assert RpcStatus.from_http(200) is RpcStatus.OK
    assert RpcStatus.from_http(204) is RpcStatus.OK


def test_unknown_http_maps_by_class() -> None:
    assert RpcStatus.from_http(418) is RpcStatus.UNKNOWN  # 4xx unknown
    assert RpcStatus.from_http(599) is RpcStatus.INTERNAL  # 5xx unknown


def test_grpc_code_values_are_stable() -> None:
    # Wire-stability: ints match gRPC canonical codes.
    assert int(RpcStatus.OK) == 0
    assert int(RpcStatus.CANCELLED) == 1
    assert int(RpcStatus.DEADLINE_EXCEEDED) == 4
    assert int(RpcStatus.UNAVAILABLE) == 14
    assert int(RpcStatus.UNAUTHENTICATED) == 16


def test_error_kind_defaults() -> None:
    assert deadline_exceeded().is_transport
    assert unavailable().is_transport
    assert cancelled().is_transport
    assert not not_found().is_transport
    assert not invalid_argument().is_transport
    assert internal().kind is FailureKind.APPLICATION


def test_error_retryable_property() -> None:
    assert unavailable().retryable
    assert resource_exhausted().retryable
    assert not not_found().retryable
    assert not invalid_argument().retryable


def test_error_dict_roundtrip() -> None:
    err = RpcError(
        RpcStatus.RESOURCE_EXHAUSTED,
        "rate limited",
        kind=FailureKind.APPLICATION,
        detail={"retry_after": 5},
        service="budget",
        method="reserve",
    )
    data = err.to_dict()
    assert data["code"] == "RESOURCE_EXHAUSTED"
    assert data["service"] == "budget"
    restored = RpcError.from_dict(data)
    assert restored.status is RpcStatus.RESOURCE_EXHAUSTED
    assert restored.detail == {"retry_after": 5}
    assert restored.method == "reserve"


def test_with_endpoint_annotation() -> None:
    err = unavailable("down")
    annotated = err.with_endpoint("memory", "query_canon")
    assert annotated.service == "memory"
    assert annotated.method == "query_canon"
    assert annotated.status is err.status
    assert annotated.kind is err.kind
