"""Anthropic (Claude) inference through the official ``anthropic`` SDK.

* Structured output: ``messages.parse(output_format=<Pydantic model>)`` - the API constrains the
  response to the schema and the SDK validates it (constraints the API cannot enforce, such as string
  lengths, are checked client-side). The result is validated again by the shared JSON base class.
* No sampling parameters (current Claude models reject ``temperature``) and no ``thinking`` parameter,
  so each model runs with its own default.
* ``stop_reason`` is checked before the output is used: a ``refusal`` or a ``max_tokens`` cut-off is
  reported as invalid output, never parsed.
* For the models that support it, server-side refusal fallback is enabled (``fallbacks: "default"``):
  if the model declines, the API re-runs the request on a fallback model inside the same call.
"""

from __future__ import annotations

from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from app.core.exceptions import ExternalAuthError, ExternalNotFound, ExternalServiceError, LLMOutputError
from app.providers.json_chat import ChatConfig, ChatReply, ContextTooLargeError, JSONChatLLM, model_check

# Models that accept the server-side refusal fallback (beta). Only used against the Anthropic API itself.
FALLBACK_MODELS = frozenset({"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
                             "claude-sonnet-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = None
        try:
            header = exc.response.headers.get("retry-after")
            retry_after = float(header) if header else None
        except (AttributeError, ValueError):
            retry_after = None
        return ExternalServiceError("Anthropic rate limit reached", service="anthropic", retryable=True,
                                    upstream_status=429, retry_after=retry_after)
    if isinstance(exc, anthropic.APITimeoutError):
        return ExternalServiceError("Anthropic request timed out", service="anthropic", retryable=True)
    if isinstance(exc, anthropic.APIConnectionError):
        return ExternalServiceError("Could not connect to Anthropic", service="anthropic", retryable=True)
    if isinstance(exc, anthropic.InternalServerError):
        return ExternalServiceError("Anthropic service error", service="anthropic", retryable=True,
                                    upstream_status=getattr(exc, "status_code", 500))
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ExternalAuthError("Anthropic rejected the API key", service="anthropic",
                                 upstream_status=getattr(exc, "status_code", 401))
    if isinstance(exc, anthropic.NotFoundError):
        return ExternalNotFound("The configured Anthropic model was not found or is unavailable to this key",
                                service="anthropic", upstream_status=404)
    if isinstance(exc, anthropic.BadRequestError):
        message = str(getattr(exc, "message", exc))
        lowered = message.lower()
        if "prompt is too long" in lowered or ("context" in lowered and "window" in lowered):
            return ContextTooLargeError("Prompt exceeds the model's context window", service="anthropic")
        return ExternalServiceError(f"Anthropic rejected the request: {message[:200]}", service="anthropic",
                                    upstream_status=400)
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 0)
        return ExternalServiceError(f"Anthropic API error {status}", service="anthropic",
                                    retryable=status >= 500, upstream_status=status)
    return exc


class AnthropicLLM(JSONChatLLM):
    provider = "anthropic"
    label = "Anthropic"

    def __init__(self, config: ChatConfig, client: Any | None = None) -> None:
        super().__init__(config)
        self._client = client or anthropic.AsyncAnthropic(
            api_key=config.api_key,
            base_url=config.base_url or None,
            timeout=config.timeout_seconds,
            max_retries=0,  # retries are handled by our bounded policy
        )
        # Fallbacks are an Anthropic API feature; skip them behind a custom base URL (proxy/other platform).
        self.use_fallbacks = self.model_name in FALLBACK_MODELS and not config.base_url

    async def _send(self, system: str, messages: list[dict[str, str]], schema: type[BaseModel]) -> ChatReply:
        request: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.config.max_output_tokens,
            "system": system,
            "messages": messages,
            "output_format": schema,
        }
        try:
            if self.use_fallbacks:
                response = await self._client.beta.messages.parse(
                    **request, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                response = await self._client.messages.parse(**request)
        except anthropic.AnthropicError as exc:
            raise _map_error(exc) from exc
        except (ValidationError, ValueError) as exc:  # SDK-side schema validation of the output
            raise LLMOutputError(f"Anthropic output failed validation: {str(exc)[:500]}") from exc

        usage = getattr(response, "usage", None)
        prompt_tokens = sum(int(getattr(usage, name, 0) or 0) for name in
                            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if response.stop_reason == "refusal":
            raise LLMOutputError("Anthropic declined this request (stop_reason: refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMOutputError("Anthropic output was cut off at max_tokens; raise LLM_MAX_OUTPUT_TOKENS")
        parsed = getattr(response, "parsed_output", None)
        if parsed is not None:
            text = parsed.model_dump_json()
        else:
            text = next((str(getattr(block, "text", "")) for block in response.content if block.type == "text"), "")
        return ChatReply(text=text, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

    async def list_models(self) -> list[str]:
        try:
            return [model.id async for model in self._client.models.list()]
        except anthropic.AnthropicError as exc:
            raise _map_error(exc) from exc

    async def diagnose(self) -> dict[str, Any]:
        result = model_check(self.label, self.model_name, await self.list_models())
        result["refusal_fallback"] = self.use_fallbacks
        return result

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()
