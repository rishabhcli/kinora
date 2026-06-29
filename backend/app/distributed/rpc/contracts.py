"""Typed service contracts — the in-Python IDL the whole mesh is built on.

A :class:`ServiceContract` is the schema-of-record for one logical service: its
name, version, and the typed signature of every method (request type → response
type, plus idempotency / streaming flags). It is the seam between the *caller's*
strongly-typed call and the *transport's* untyped ``dict`` payload: the contract
**encodes** a typed request to a JSON-ready dict on the way out and **decodes** a
dict back to the typed response on the way in, validating both ends. That is what
makes the layer safe to flip from in-process to a real wire — the bytes on the
wire are exactly what the contract promised, on both sides, regardless of who is
serving.

Defining a contract reads like an IDL:

    >>> import dataclasses
    >>> @dataclasses.dataclass
    ... class PlanShotReq:
    ...     shot_hash: str
    ...     beat_id: str
    >>> @dataclasses.dataclass
    ... class PlanShotResp:
    ...     spec: dict
    >>> cinematographer = ServiceContract.define(
    ...     "cinematographer",
    ...     version=1,
    ...     methods=[
    ...         method("plan_shot", PlanShotReq, PlanShotResp, idempotent=True),
    ...     ],
    ... )
    >>> cinematographer.method("plan_shot").idempotent
    True

The codec supports **pydantic models** (``model_dump`` / ``model_validate``),
**dataclasses**, and plain ``dict`` / scalar types — so the existing packages can
expose their already-defined pydantic DTOs as method types with zero rewrites.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, get_type_hints

from app.distributed.rpc.errors import RpcError, invalid_argument

# A method type may be a pydantic model class, a dataclass, ``dict``, ``None``
# (no body), or a scalar (``str`` / ``int`` / ``float`` / ``bool``).
TypeSpec = type | None


def _is_pydantic_model(tp: Any) -> bool:
    """True if ``tp`` is a pydantic v2 ``BaseModel`` subclass (duck-typed)."""
    return (
        isinstance(tp, type)
        and hasattr(tp, "model_validate")
        and hasattr(tp, "model_dump")
        and hasattr(tp, "model_fields")
    )


def encode_value(value: Any, type_spec: TypeSpec) -> Any:
    """Encode a typed ``value`` into a JSON-ready payload per ``type_spec``.

    * ``None`` type / value → ``None`` (no body).
    * pydantic model → ``model_dump(mode="json")``.
    * dataclass → ``dataclasses.asdict``.
    * dict / scalar → passed through (shallow-copied for dict).
    """
    if type_spec is None or value is None:
        return None
    if _is_pydantic_model(type(value)):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [encode_value(v, None) for v in value]
    raise invalid_argument(f"cannot encode value of type {type(value).__name__}")


def decode_value(payload: Any, type_spec: TypeSpec) -> Any:
    """Decode a wire ``payload`` into the typed value declared by ``type_spec``.

    Validation failures (a payload that does not satisfy the declared type)
    surface as :class:`RpcError` ``INVALID_ARGUMENT`` so a malformed request /
    response is a clean client error, not a server crash.
    """
    if type_spec is None:
        return None
    if _is_pydantic_model(type_spec):
        try:
            return type_spec.model_validate(payload)  # type: ignore[attr-defined]
        except Exception as exc:  # pydantic.ValidationError (avoid hard import)
            raise invalid_argument(f"payload failed validation: {exc}") from exc
    if isinstance(type_spec, type) and dataclasses.is_dataclass(type_spec):
        if not isinstance(payload, Mapping):
            raise invalid_argument(f"expected an object for {type_spec.__name__}")
        return _build_dataclass(type_spec, payload)
    if type_spec is dict:
        if not isinstance(payload, Mapping):
            raise invalid_argument("expected an object payload")
        return dict(payload)
    if type_spec in (str, int, float, bool):
        if payload is None:
            raise invalid_argument(f"expected a {type_spec.__name__}, got null")
        return type_spec(payload)
    # Unknown type spec: pass the payload through untouched.
    return payload


def _build_dataclass(cls: type, payload: Mapping[str, Any]) -> Any:
    """Construct a dataclass from a mapping, accepting only declared fields."""
    try:
        hints = get_type_hints(cls)
    except Exception:  # forward refs that can't resolve; fall back to raw fields
        hints = {}
    valid = {f.name for f in dataclasses.fields(cls)}
    unknown = set(payload) - valid
    if unknown:
        raise invalid_argument(f"unknown fields for {cls.__name__}: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in payload:
            sub = hints.get(f.name)
            raw = payload[f.name]
            # Recurse into nested dataclasses / pydantic models.
            if sub is not None and (
                _is_pydantic_model(sub) or (isinstance(sub, type) and dataclasses.is_dataclass(sub))
            ):
                kwargs[f.name] = decode_value(raw, sub)
            else:
                kwargs[f.name] = raw
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise invalid_argument(f"cannot build {cls.__name__}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """The typed signature of one contract method.

    ``request_type`` / ``response_type`` drive encode/decode. ``idempotent``
    declares whether the method is safe to **retry / hedge** (a read, or a write
    keyed by an idempotency key like ``shot_hash`` §12.1) — the client's
    resilience policies consult this so they never blindly re-issue a
    non-idempotent write. ``streaming`` marks a server-streaming method
    (reserved for the event channel §5.6; the unary path is the default).
    """

    name: str
    request_type: TypeSpec = None
    response_type: TypeSpec = None
    idempotent: bool = False
    streaming: bool = False
    description: str = ""

    def encode_request(self, value: Any) -> dict[str, Any]:
        """Encode a typed request to a JSON-ready dict payload."""
        encoded = encode_value(value, self.request_type)
        if encoded is None:
            return {}
        if not isinstance(encoded, dict):
            # Wrap a scalar/list body so the payload is always an object on the
            # wire (transports key on dict payloads).
            return {"_value": encoded}
        return encoded

    def decode_request(self, payload: dict[str, Any]) -> Any:
        """Decode a wire payload back to the typed request."""
        if self.request_type is None:
            return None
        if "_value" in payload and len(payload) == 1 and not _is_pydantic_model(self.request_type):
            return decode_value(payload["_value"], self.request_type)
        return decode_value(payload, self.request_type)

    def encode_response(self, value: Any) -> Any:
        """Encode a typed response to a JSON-ready body."""
        return encode_value(value, self.response_type)

    def decode_response(self, body: Any) -> Any:
        """Decode a wire body back to the typed response."""
        return decode_value(body, self.response_type)


def method(
    name: str,
    request_type: TypeSpec = None,
    response_type: TypeSpec = None,
    *,
    idempotent: bool = False,
    streaming: bool = False,
    description: str = "",
) -> MethodSpec:
    """Convenience factory for a :class:`MethodSpec` (reads like an IDL line)."""
    return MethodSpec(
        name=name,
        request_type=request_type,
        response_type=response_type,
        idempotent=idempotent,
        streaming=streaming,
        description=description,
    )


@dataclass(frozen=True)
class ServiceContract:
    """The schema-of-record for one logical service.

    A contract is *data*, not behaviour: it names the service, pins a ``version``,
    and maps each method name to its :class:`MethodSpec`. The server binds an
    implementation to it; the client generates a typed stub from it; the registry
    indexes it. Two endpoints claiming the same ``(name, version)`` must agree on
    the method set — :meth:`fingerprint` gives a stable hash to assert that.
    """

    name: str
    version: int
    methods: Mapping[str, MethodSpec] = field(default_factory=dict)
    description: str = ""

    @classmethod
    def define(
        cls,
        name: str,
        *,
        version: int = 1,
        methods: list[MethodSpec],
        description: str = "",
    ) -> ServiceContract:
        """Define a contract from a list of method specs (rejects duplicates)."""
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"invalid service name: {name!r}")
        by_name: dict[str, MethodSpec] = {}
        for spec in methods:
            if spec.name in by_name:
                raise ValueError(f"duplicate method {spec.name!r} in service {name!r}")
            by_name[spec.name] = spec
        return cls(name=name, version=version, methods=by_name, description=description)

    @property
    def qualified_name(self) -> str:
        """The ``name@vN`` identifier used by the registry / discovery."""
        return f"{self.name}@v{self.version}"

    def method(self, name: str) -> MethodSpec:
        """Look up a method spec or raise ``INVALID_ARGUMENT`` if absent."""
        spec = self.methods.get(name)
        if spec is None:
            raise _no_method(self.name, name)
        return spec

    def has_method(self, name: str) -> bool:
        """Whether the contract declares ``name``."""
        return name in self.methods

    def fingerprint(self) -> str:
        """A stable hash of the method surface (assert two endpoints agree).

        Independent of method declaration order, so the same contract built in two
        places fingerprints identically — a cheap compatibility check before a
        client trusts an endpoint claiming to serve this contract.
        """
        import hashlib

        parts = [f"{self.name}@v{self.version}"]
        for mname in sorted(self.methods):
            spec = self.methods[mname]
            req = _type_name(spec.request_type)
            resp = _type_name(spec.response_type)
            parts.append(f"{mname}:{req}->{resp}:i={int(spec.idempotent)}:s={int(spec.streaming)}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _no_method(service: str, name: str) -> RpcError:
    return invalid_argument(f"service {service!r} has no method {name!r}")


def _type_name(tp: TypeSpec) -> str:
    if tp is None:
        return "void"
    return getattr(tp, "__name__", str(tp))


__all__ = [
    "MethodSpec",
    "ServiceContract",
    "TypeSpec",
    "decode_value",
    "encode_value",
    "method",
]
