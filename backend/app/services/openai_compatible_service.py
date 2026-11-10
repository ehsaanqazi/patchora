"""OpenAI, DeepSeek and Google Gemini inference through the official ``openai`` SDK.

DeepSeek and Gemini both serve OpenAI-compatible Chat Completions endpoints, so one client covers
all three; only the base URL, the output-token parameter name and sampling support differ.
Output uses JSON mode (``response_format={"type": "json_object"}``) and is always validated with
Pydantic by :class:`app.providers.json_chat.JSONChatLLM`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import openai
from pydantic import BaseModel

from app.core.exceptions import ExternalAuthError, ExternalNotFound, ExternalServiceError, LLMOutputError
from app.providers.json_chat import ChatConfig, ChatReply, ContextTooLargeError, JSONChatLLM, model_check


@dataclass(frozen=True)
class CompatProfile:
    provider: str
    label: str
    default_base_url: str | None
    max_tokens_param: str  # OpenAI's current models take max_completion_tokens; the others max_tokens
    send_temperature: bool  # OpenAI reasoning models reject a non-default temperature


PROFILES: dict[str, CompatProfile] = {
    "openai": CompatProfile("openai", "OpenAI", None, "max_completion_tokens", send_temperature=False),
    "deepseek": CompatProfile("deepseek", "DeepSeek", "https://api.deepseek.com", "max_tokens", True),
    "gemini": CompatProfile("gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
                            "max_tokens", True),
}


def _map_error(exc: Exception, provider: str, label: str) -> Exception:
    if isinstance(exc, openai.RateLimitError):
        retry_after = None
        try:
            header = exc.response.headers.get("retry-after")
            retry_after = float(header) if header else None
        except (AttributeError, ValueError):
            retry_after = None
        return ExternalServiceError(f"{label} rate limit reached", service=provider, retryable=True,
                                    upstream_status=429, retry_after=retry_after)
    if isinstance(exc, openai.APITimeoutError):
        return ExternalServiceError(f"{label} request timed out", service=provider, retryable=True)
    if isinstance(exc, openai.APIConnectionError):
        return ExternalServiceError(f"Could not connect to {label}", service=provider, retryable=True)
    if isinstance(exc, openai.InternalServerError):
        return ExternalServiceError(f"{label} service error", service=provider, retryable=True,
                                    upstream_status=getattr(exc, "status_code", 500))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ExternalAuthError(f"{label} rejected the API key", service=provider,
                                 upstream_status=getattr(exc, "status_code", 401))
    if isinstance(exc, openai.NotFoundError):
        return ExternalNotFound(f"The configured {label} model was not found or is unavailable to this key",
                                service=provider, upstream_status=404)
    if isinstance(exc, openai.BadRequestError):
        message = str(getattr(exc, "message", exc))
        lowered = message.lower()
        if ("context" in lowered and ("length" in lowered or "window" in lowered)) or "too long" in lowered \
                or "maximum context" in lowered:
            return ContextTooLargeError("Prompt exceeds the model's context window", service=provider)
        if "json" in lowered and ("generate" in lowered or "valid" in lowered):
            return LLMOutputError(f"{label} could not produce valid JSON for this request")
        return ExternalServiceError(f"{label} rejected the request: {message[:200]}", service=provider,
                                    upstream_status=400)
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0)
        return ExternalServiceError(f"{label} API error {status}", service=provider,
                                    retryable=status >= 500, upstream_status=status)
    return exc


class OpenAICompatibleLLM(JSONChatLLM):
    def __init__(self, provider: str, config: ChatConfig, client: Any | None = None) -> None:
        self.profile = PROFILES[provider]
        self.provider = provider
        self.label = self.profile.label
        super().__init__(config)
        self._client = client or openai.AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url or self.profile.default_base_url,
            timeout=config.timeout_seconds,
            max_retries=0,  # retries are handled by our bounded policy
        )

    async def _send(self, system: str, messages: list[dict[str, str]], schema: type[BaseModel]) -> ChatReply:
        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": system}, *messages],
            "response_format": {"type": "json_object"},
            self.profile.max_tokens_param: self.config.max_output_tokens,
        }
        if self.profile.send_temperature and self.config.temperature is not None:
            request["temperature"] = self.config.temperature
        try:
            response = await self._client.chat.completions.create(**request)
        except openai.OpenAIError as exc:
            raise _map_error(exc, self.provider, self.label) from exc
        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise LLMOutputError(f"{self.label} returned no choices")
        usage = getattr(response, "usage", None)
        return ChatReply(text=choice.message.content or "",
                         prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                         completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0))

    async def list_models(self) -> list[str]:
        try:
            page = await self._client.models.list()
        except openai.OpenAIError as exc:
            raise _map_error(exc, self.provider, self.label) from exc
        ids = []
        for model in getattr(page, "data", []) or []:
            model_id = getattr(model, "id", None)
            if model_id:
                # Gemini lists ids as "models/<id>" but accepts the bare id in requests.
                ids.append(model_id.removeprefix("models/"))
        return ids

    async def diagnose(self) -> dict[str, Any]:
        return model_check(self.label, self.model_name, await self.list_models())

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()
