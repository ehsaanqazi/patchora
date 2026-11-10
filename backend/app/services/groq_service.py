"""Groq inference client.

* The model id comes from configuration (``GROQ_MODEL`` or the Settings page) - nothing is
  hard-coded. ``diagnose()`` verifies that the configured model is available to the key and
  reports its context window.
* Structured output: JSON mode (``response_format={"type": "json_object"}``) by default, or
  ``json_schema`` for models that support it. Output is always validated with Pydantic and
  re-requested (bounded) with the validation error when invalid.
* Retries only retryable failures (429 honoring ``retry-after``, timeouts, connection errors,
  5xx). Authentication and bad-request errors fail immediately.
* The API key is never logged; only token counts, model name and duration are recorded.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import groq
from pydantic import BaseModel

from app.core.exceptions import (
    ExternalAuthError,
    ExternalNotFound,
    ExternalServiceError,
    LLMOutputError,
    NotConfiguredError,
)
from app.core.logging import get_logger
from app.providers.llm import LLMResult, validate_output
from app.workers.retry_policy import RetryPolicy, retry_async

logger = get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

CHARS_PER_TOKEN = 3.5


@dataclass
class GroqConfig:
    api_key: str | None
    model: str | None
    base_url: str | None = None
    timeout_seconds: float = 60.0
    max_retries: int = 3
    max_input_tokens: int = 24_000
    max_output_tokens: int = 4_096
    structured_output_mode: str = "json_object"
    temperature: float = 0.1


class ContextTooLargeError(ExternalServiceError):
    code = "context_too_large"


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, groq.RateLimitError):
        retry_after = None
        try:
            header = exc.response.headers.get("retry-after")
            retry_after = float(header) if header else None
        except (AttributeError, ValueError):
            retry_after = None
        return ExternalServiceError("Groq rate limit reached", service="groq", retryable=True,
                                    upstream_status=429, retry_after=retry_after)
    if isinstance(exc, groq.APITimeoutError):
        return ExternalServiceError("Groq request timed out", service="groq", retryable=True)
    if isinstance(exc, groq.APIConnectionError):
        return ExternalServiceError("Could not connect to Groq", service="groq", retryable=True)
    if isinstance(exc, groq.InternalServerError):
        return ExternalServiceError("Groq service error", service="groq", retryable=True,
                                    upstream_status=getattr(exc, "status_code", 500))
    if isinstance(exc, (groq.AuthenticationError, groq.PermissionDeniedError)):
        return ExternalAuthError("Groq rejected the API key (check GROQ_API_KEY)", service="groq",
                                 upstream_status=getattr(exc, "status_code", 401))
    if isinstance(exc, groq.NotFoundError):
        return ExternalNotFound("The configured Groq model was not found or is unavailable to this key",
                                service="groq", upstream_status=404)
    if isinstance(exc, groq.BadRequestError):
        message = str(getattr(exc, "message", exc))
        lowered = message.lower()
        if "json_validate_failed" in lowered or "failed to generate json" in lowered:
            return LLMOutputError("Groq could not produce valid JSON for this request")
        if ("context" in lowered and ("length" in lowered or "window" in lowered)) or "too large" in lowered:
            return ContextTooLargeError("Prompt exceeds the model's context window", service="groq")
        return ExternalServiceError(f"Groq rejected the request: {message[:200]}", service="groq",
                                    upstream_status=400)
    if isinstance(exc, groq.APIStatusError):
        status = getattr(exc, "status_code", 0)
        return ExternalServiceError(f"Groq API error {status}", service="groq",
                                    retryable=status >= 500, upstream_status=status)
    return exc


class GroqLLM:
    provider = "groq"
    label = "Groq"
    simulated = False

    def __init__(self, config: GroqConfig, client: groq.AsyncGroq | None = None) -> None:
        if not config.api_key:
            raise NotConfiguredError("GROQ_API_KEY is not configured.")
        if not config.model:
            raise NotConfiguredError(
                "GROQ_MODEL is not configured. Choose a currently available Groq chat model "
                "(see the Integrations page's model list)."
            )
        self.config = config
        self.model_name = config.model
        self._client = client or groq.AsyncGroq(
            api_key=config.api_key,
            base_url=config.base_url or None,
            timeout=config.timeout_seconds,
            max_retries=0,  # retries are handled by our bounded policy below
        )

    @property
    def input_budget_chars(self) -> int:
        return int(self.config.max_input_tokens * CHARS_PER_TOKEN)

    def _response_format(self, schema: type[BaseModel]) -> dict[str, Any]:
        if self.config.structured_output_mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(),
                                "strict": False},
            }
        return {"type": "json_object"}

    async def _call(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> Any:
        async def _do() -> Any:
            try:
                request: dict[str, Any] = {
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": self.config.temperature,
                    "max_completion_tokens": self.config.max_output_tokens,
                    "response_format": self._response_format(schema),
                }
                create: Any = self._client.chat.completions.create
                return await create(**request)
            except groq.GroqError as exc:
                raise _map_error(exc) from exc

        return await retry_async(_do, RetryPolicy(max_attempts=self.config.max_retries, base_delay=1.0))

    async def complete_json(self, *, system: str, user: str, schema: type[T], purpose: str) -> LLMResult[T]:
        schema_text = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        system_full = (
            f"{system}\n\nRespond with a single JSON object only, conforming to this JSON schema:\n"
            f"{schema_text}"
        )
        estimated = (len(system_full) + len(user)) / CHARS_PER_TOKEN
        if estimated > self.config.max_input_tokens:
            raise ContextTooLargeError(
                f"Prompt (~{int(estimated)} tokens) exceeds GROQ_MAX_INPUT_TOKENS", service="groq"
            )
        messages = [{"role": "system", "content": system_full}, {"role": "user", "content": user}]
        prompt_tokens = completion_tokens = 0
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(1, 3):  # one corrective re-ask on invalid output
            try:
                response = await self._call(messages, schema)
            except LLMOutputError as exc:
                last_error = exc
                continue
            usage = getattr(response, "usage", None)
            prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            content = response.choices[0].message.content or ""
            try:
                data = validate_output(content, schema)
            except LLMOutputError as exc:
                last_error = exc
                messages = [
                    *messages,
                    {"role": "assistant", "content": content[:4000]},
                    {"role": "user", "content": "Your previous output did not match the schema: "
                                                f"{str(exc)[:800]}. Return only the corrected JSON object."},
                ]
                continue
            duration = int((time.monotonic() - started) * 1000)
            logger.info("groq completion", extra={"purpose": purpose, "model": self.model_name,
                                                  "prompt_tokens": prompt_tokens,
                                                  "completion_tokens": completion_tokens,
                                                  "duration_ms": duration, "attempts": attempt})
            return LLMResult(data=data, model=self.model_name, prompt_tokens=prompt_tokens,
                             completion_tokens=completion_tokens, duration_ms=duration, attempts=attempt)
        raise LLMOutputError(f"Model output invalid after retries: {last_error}")

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            result = await self._client.models.list()
        except groq.GroqError as exc:
            raise _map_error(exc) from exc
        models = []
        for model in getattr(result, "data", []) or []:
            models.append({
                "id": getattr(model, "id", None),
                "owned_by": getattr(model, "owned_by", None),
                "active": getattr(model, "active", None),
                "context_window": getattr(model, "context_window", None),
            })
        return models

    async def diagnose(self) -> dict[str, Any]:
        """Verify the key and configured model without exposing the key."""
        models = await self.list_models()
        match = next((m for m in models if m["id"] == self.model_name), None)
        return {
            "key_valid": True,
            "model": self.model_name,
            "model_available": match is not None and match.get("active") is not False,
            "context_window": match.get("context_window") if match else None,
            "structured_output_mode": self.config.structured_output_mode,
            "available_models": sorted(m["id"] for m in models if m["id"]),
        }


def estimate_cost(prompt_tokens: int, completion_tokens: int, price_in: float | None,
                  price_out: float | None) -> float | None:
    """Only computed when pricing is explicitly configured; never guessed."""
    if price_in is None or price_out is None:
        return None
    return round(prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out, 6)
