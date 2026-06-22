"""``BaseAgent`` — the shared contract-bound, JSON-strict agent runtime (§7.1, §10).

Every creative agent is a thin service behind a typed Pydantic contract. This
base class owns the mechanics they all share:

* a model id + a versioned system prompt (so each message is tagged with the
  exact prompt revision that produced it);
* :meth:`run_json` — a JSON-strict text call (``providers.chat.chat_json``) that
  validates the reply into a response model and, on a *validation* error, does
  exactly ONE repair round-trip ("your JSON failed validation: …; return
  corrected JSON only");
* :meth:`run_json_vl` — the same, but multimodal (``providers.vl.analyze_json``)
  for the agents that look at pixels;
* :meth:`run_tool_loop` — optional Qwen function-calling driven by the MCP
  skills dispatcher (``canon.query`` / ``shot.render`` etc., §8.3);
* structured logging of agent name + prompt version + token usage on every call.

Deterministic policy logic (the §9.3 render-mode tree, the §9.5 Critic routing,
the §7.2 arbitration policy) lives in the concrete agents as pure functions, so
it is unit-testable without ever touching this runtime.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.providers import Providers
from app.providers.chat import extract_json

from .prompts import VersionedPrompt

if TYPE_CHECKING:
    from app.mcp.skills import QwenSkillDispatcher

logger = get_logger("app.agents")

TModel = TypeVar("TModel", bound=BaseModel)

#: Payload accepted by the agent entry points (serialized to the user turn).
Payload = BaseModel | dict[str, Any] | list[Any] | str

_MAX_REPORTED_ERRORS = 8


def _format_validation_errors(exc: ValidationError) -> str:
    """Render a terse, model-readable summary of why validation failed."""
    parts: list[str] = []
    for err in exc.errors()[:_MAX_REPORTED_ERRORS]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


class BaseAgent:
    """A contract-bound agent: model + versioned prompt + JSON-strict calling."""

    def __init__(
        self,
        providers: Providers,
        *,
        name: str,
        model: str,
        prompt: VersionedPrompt,
        skills: QwenSkillDispatcher | None = None,
    ) -> None:
        self._providers = providers
        self.name = name
        self.model = model
        self.prompt = prompt
        self._skills = skills
        self._log = logger.bind(agent=name, prompt_version=prompt.version, model=model)

    # -- public entry points ------------------------------------------------- #

    async def run_json(
        self,
        payload: Payload,
        response_model: type[TModel],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> TModel:
        """Run a JSON-strict text completion and validate it into ``response_model``.

        On a Pydantic validation error, a single repair round-trip is attempted;
        a second failure propagates.
        """
        messages = self._messages(payload)
        before = self._token_total()
        raw = await self._providers.chat.chat_json(
            messages, self.model, temperature=temperature, max_tokens=max_tokens, tools=tools
        )
        repaired = False
        try:
            model = response_model.model_validate(raw)
        except ValidationError as exc:
            repaired = True
            model = await self._repair_text(
                messages, raw, exc, response_model, temperature, max_tokens, tools
            )
        self._log_call(response_model, before, repaired=repaired, modality="text")
        return model

    async def run_json_vl(
        self,
        images: list[bytes | str],
        payload: Payload,
        response_model: type[TModel],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TModel:
        """Multimodal counterpart of :meth:`run_json` (looks at clip frames/pages)."""
        prompt = f"{self.prompt.system}\n\n{self._user_content(payload)}"
        before = self._token_total()
        raw = await self._providers.vl.analyze_json(
            images, prompt, model=self.model, temperature=temperature, max_tokens=max_tokens
        )
        repaired = False
        try:
            model = response_model.model_validate(raw)
        except ValidationError as exc:
            repaired = True
            reminder = (
                f"{prompt}\n\nYour JSON failed validation: "
                f"{_format_validation_errors(exc)}. Return corrected JSON only."
            )
            raw2 = await self._providers.vl.analyze_json(
                images, reminder, model=self.model, temperature=temperature, max_tokens=max_tokens
            )
            model = response_model.model_validate(raw2)
        self._log_call(response_model, before, repaired=repaired, modality="vl")
        return model

    async def run_tool_loop(
        self,
        payload: Payload,
        response_model: type[TModel],
        *,
        tools: list[dict[str, Any]],
        max_rounds: int = 4,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TModel:
        """Drive Qwen function-calling against the MCP skills, then parse JSON.

        The model may call ``canon.query`` / ``shot.render`` etc. (the featured
        skills, §8.3); each call is dispatched and its result fed back until the
        model returns a final JSON answer, which is validated into
        ``response_model``.
        """
        if self._skills is None:
            raise ValueError("run_tool_loop requires a QwenSkillDispatcher (skills=...)")
        messages = self._messages(payload)
        before = self._token_total()
        for _ in range(max_rounds):
            result = await self._providers.chat.chat(
                messages, self.model, temperature=temperature, max_tokens=max_tokens, tools=tools
            )
            if result.tool_calls:
                messages.append(self._assistant_tool_turn(result.tool_calls, result.text))
                for call in result.tool_calls:
                    output = await self._skills.dispatch(call.name, call.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id or call.name,
                            "content": json.dumps(output),
                        }
                    )
                continue
            model = response_model.model_validate(extract_json(result.text))
            self._log_call(response_model, before, repaired=False, modality="tools")
            return model
        raise RuntimeError(
            f"{self.name}: tool loop exhausted {max_rounds} rounds without an answer"
        )

    # -- internals ----------------------------------------------------------- #

    def _messages(self, payload: Payload) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": self.prompt.system},
            {"role": "user", "content": self._user_content(payload)},
        ]

    @staticmethod
    def _user_content(payload: Payload) -> str:
        if isinstance(payload, BaseModel):
            return payload.model_dump_json()
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, default=str)

    async def _repair_text(
        self,
        messages: list[dict[str, Any]],
        bad_raw: Any,
        exc: ValidationError,
        response_model: type[TModel],
        temperature: float | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
    ) -> TModel:
        reminder = (
            f"Your JSON failed validation: {_format_validation_errors(exc)}. "
            "Return corrected JSON only."
        )
        repair_messages = [
            *messages,
            {"role": "assistant", "content": json.dumps(bad_raw, default=str)},
            {"role": "user", "content": reminder},
        ]
        raw = await self._providers.chat.chat_json(
            repair_messages,
            self.model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )
        return response_model.model_validate(raw)

    @staticmethod
    def _assistant_tool_turn(tool_calls: list[Any], text: str) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {
                    "id": tc.id or tc.name,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ],
        }

    def _token_total(self) -> int | None:
        client = getattr(self._providers, "client", None)
        totals = getattr(client, "usage_totals", None) if client is not None else None
        return totals.total_tokens if totals is not None else None

    def _log_call(
        self,
        response_model: type[BaseModel],
        tokens_before: int | None,
        *,
        repaired: bool,
        modality: str,
    ) -> None:
        tokens_after = self._token_total()
        delta = (
            tokens_after - tokens_before
            if tokens_after is not None and tokens_before is not None
            else None
        )
        self._log.info(
            "agent.call",
            response_model=response_model.__name__,
            modality=modality,
            repaired=repaired,
            tokens=delta,
        )


__all__ = ["BaseAgent", "Payload", "TModel"]
