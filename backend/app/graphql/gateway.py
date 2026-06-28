"""The request orchestrator: parse → persisted → validate → execute.

:func:`run_graphql` is the single entry point that turns a raw GraphQL HTTP body
(``query``/``variables``/``operationName`` or a persisted-query ``id``/APQ
extension) into a GraphQL response dict, applying — in order — the persisted-query
resolution, parse, depth/complexity validation, and async execution with the
per-request context. Every failure is collected as a masked GraphQL error so the
HTTP layer can always return a well-formed ``{"data":…, "errors":…}`` body.

Per-key rate limiting is charged once per request, costed by the operation's
static complexity, so an expensive query spends proportionally more of the
bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.graphql.context import GraphQLContext, build_context
from app.graphql.errors import GraphQLError, bad_input, mask_error
from app.graphql.execute import ExecutionResult, execute, select_operation
from app.graphql.language import parse
from app.graphql.language.parser import GraphQLSyntaxError
from app.graphql.persisted import (
    PersistedQueryStore,
    extract_apq_hash,
    resolve_query_text,
)
from app.graphql.schema import Schema
from app.graphql.validate import ValidationLimits, estimate_cost, validate


@dataclass(slots=True)
class GraphQLRequest:
    """A normalized GraphQL HTTP request body."""

    query: str | None = None
    variables: dict[str, Any] | None = None
    operation_name: str | None = None
    persisted_id: str | None = None
    extensions: dict[str, Any] | None = None

    @classmethod
    def from_body(cls, body: Any) -> GraphQLRequest:
        if not isinstance(body, dict):
            raise bad_input("Request body must be a JSON object.")
        variables = body.get("variables")
        if variables is not None and not isinstance(variables, dict):
            raise bad_input("`variables` must be an object.")
        return cls(
            query=body.get("query") if isinstance(body.get("query"), str) else None,
            variables=variables,
            operation_name=(
                body.get("operationName")
                if isinstance(body.get("operationName"), str)
                else None
            ),
            persisted_id=body.get("id") if isinstance(body.get("id"), str) else None,
            extensions=(
                body.get("extensions") if isinstance(body.get("extensions"), dict) else None
            ),
        )


async def run_graphql(
    *,
    schema: Schema,
    request: GraphQLRequest,
    context: GraphQLContext,
    persisted: PersistedQueryStore,
    limits: ValidationLimits | None = None,
    rate_limit: Any | None = None,
) -> dict[str, Any]:
    """Run one GraphQL request end-to-end, returning the response dict."""
    limits = limits or ValidationLimits()
    try:
        query = await resolve_query_text(
            persisted,
            query=request.query,
            persisted_id=request.persisted_id,
            apq_hash=extract_apq_hash(request.extensions),
        )
    except GraphQLError as exc:
        return ExecutionResult(data=None, errors=[exc]).to_response()

    try:
        document = parse(query)
    except GraphQLSyntaxError as exc:
        from app.graphql.errors import ErrorCode

        err = GraphQLError(
            exc.message,
            code=ErrorCode.GRAPHQL_PARSE_FAILED,
            locations=[(exc.line, exc.column)],
        )
        return ExecutionResult(data=None, errors=[err]).to_response()

    validation_errors = validate(schema, document, limits=limits)
    if validation_errors:
        return ExecutionResult(data=None, errors=validation_errors).to_response()

    # Per-key rate limit, charged by the operation's static cost.
    if rate_limit is not None:
        try:
            operation = select_operation(document, request.operation_name)
            cost = max(1, estimate_cost(schema, document, operation))
            await rate_limit.check(context.api_key, cost=min(cost, context.api_key.rpm))
        except GraphQLError as exc:
            return ExecutionResult(data=None, errors=[exc]).to_response()

    try:
        result = await execute(
            schema,
            document,
            operation_name=request.operation_name,
            variables=request.variables or {},
            context=context,
        )
    except Exception as exc:  # noqa: BLE001 - last-resort mask
        return ExecutionResult(data=None, errors=[mask_error(exc)]).to_response()
    return result.to_response()


async def build_request_context(container: Any, api_key: Any) -> GraphQLContext:
    """Build a per-request context (thin wrapper over ``context.build_context``)."""
    return build_context(container, api_key)


__all__ = ["GraphQLRequest", "build_request_context", "run_graphql"]
