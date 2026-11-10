"""Shared behaviour for real LLM providers that return JSON as text.

Subclasses implement one request (:meth:`JSONChatLLM._send`) and their own error mapping; this base
adds what every provider must do the same way:

* the JSON schema is stated in the system prompt and the output is validated with Pydantic;
* invalid output is re-requested once with the validation error (bounded);
* only retryable failures (rate limits, timeouts, connection errors, 5xx) are retried, with backoff;
* the prompt is checked against the configured input budget before anything is sent;
* only token counts, model name and duration are logged - never keys or prompt content.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.exceptions import ExternalServiceError, LLMOutputError, NotConfiguredError
from app.core.logging import get_logger
from app.providers.llm import LLMResult, validate_output
from app.workers.retry_policy import RetryPolicy, retry_async

logger = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

CHARS_PER_TOKEN = 3.5


class ContextTooLargeError(ExternalServiceError):
    code = "context_too_large"


@dataclass
class ChatConfig:
    api_key: str | None
    model: str | None
    base_url: str | None = None
    timeout_seconds: float = 120.0
    max_retries: int = 3
    max_input_tokens: int = 100_000
    max_output_tokens: int = 16_000
    temperature: float | None = 0.1


@dataclass
class ChatReply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class JSONChatLLM:
    """Base class; subclasses set ``provider``/``label`` and implement ``_send`` and ``diagnose``."""

    provider = "llm"
    label = "LLM"
    simulated = False
    requires_api_key = True

    def __init__(self, config: ChatConfig) -> None:
        if self.requires_api_key and not config.api_key:
            raise NotConfiguredError(f"{self.label} API key is not configured. Add it on the Integrations page.")
        if not config.model:
            raise NotConfiguredError(f"{self.label} model is not configured. Set a model id on the Integrations "
                                     "page ('Test connection' lists the models available to your key).")
        self.config = config
        self.model_name = config.model

    @property
    def input_budget_chars(self) -> int:
        return int(self.config.max_input_tokens * CHARS_PER_TOKEN)

    # --- provider-specific ---------------------------------------------------------------

    async def _send(self, system: str, messages: list[dict[str, str]], schema: type[BaseModel]) -> ChatReply:
        raise NotImplementedError

    async def diagnose(self) -> dict[str, Any]:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None

    # --- shared ----------------------------------------------------------------------------

    def _system_with_schema(self, system: str, schema: type[BaseModel]) -> str:
        schema_text = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        return (f"{system}\n\nRespond with a single JSON object only, conforming to this JSON schema:\n"
                f"{schema_text}")

    def _check_budget(self, system: str, user: str) -> None:
        estimated = (len(system) + len(user)) / CHARS_PER_TOKEN
        if estimated > self.config.max_input_tokens:
            raise ContextTooLargeError(
                f"Prompt (~{int(estimated)} tokens) exceeds the configured input budget "
                f"({self.config.max_input_tokens} tokens)", service=self.provider)

    async def _send_with_retry(self, system: str, messages: list[dict[str, str]],
                               schema: type[BaseModel]) -> ChatReply:
        return await retry_async(lambda: self._send(system, messages, schema),
                                 RetryPolicy(max_attempts=max(1, self.config.max_retries), base_delay=1.0))

    async def complete_json(self, *, system: str, user: str, schema: type[T], purpose: str) -> LLMResult[T]:
        system_full = self._system_with_schema(system, schema)
        self._check_budget(system_full, user)
        messages = [{"role": "user", "content": user}]
        prompt_tokens = completion_tokens = 0
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(1, 3):  # one corrective re-ask on invalid output
            try:
                reply = await self._send_with_retry(system_full, messages, schema)
            except LLMOutputError as exc:
                last_error = exc
                continue
            prompt_tokens += reply.prompt_tokens
            completion_tokens += reply.completion_tokens
            try:
                data = validate_output(reply.text, schema)
            except LLMOutputError as exc:
                last_error = exc
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply.text[:4000] or "{}"},
                    {"role": "user", "content": "Your previous output did not match the schema: "
                                                f"{str(exc)[:800]}. Return only the corrected JSON object."},
                ]
                continue
            duration = int((time.monotonic() - started) * 1000)
            logger.info("llm completion", extra={"provider": self.provider, "purpose": purpose,
                                                 "model": self.model_name, "prompt_tokens": prompt_tokens,
                                                 "completion_tokens": completion_tokens, "duration_ms": duration,
                                                 "attempts": attempt})
            return LLMResult(data=data, model=self.model_name, prompt_tokens=prompt_tokens,
                             completion_tokens=completion_tokens, duration_ms=duration, attempts=attempt)
        raise LLMOutputError(f"Model output invalid after retries: {last_error}")


def model_check(provider_label: str, model: str, available: list[str]) -> dict[str, Any]:
    """Standard ``diagnose()`` result for providers that can list their models."""
    return {
        "key_valid": True,
        "model": model,
        "model_available": model in available,
        "available_models": sorted(available),
        "provider": provider_label,
    }
