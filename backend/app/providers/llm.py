"""LLM client interface and the scripted implementation.

The agent depends only on :class:`LLMClient`. Implementations:

* :class:`app.services.groq_service.GroqLLM` - real Groq inference.
* :class:`ScriptedLLM` - deterministic, pre-recorded responses for demo mode, automated tests
  and pipeline evaluation. Every result it produces is flagged ``simulated=True`` and the UI
  labels it as such.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.exceptions import LLMOutputError

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult[M: BaseModel]:
    data: M
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    attempts: int = 1
    simulated: bool = False


class LLMClient(Protocol):
    model_name: str
    simulated: bool

    @property
    def input_budget_chars(self) -> int: ...

    async def complete_json(
        self, *, system: str, user: str, schema: type[T], purpose: str
    ) -> LLMResult[T]: ...


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


def extract_json_object(text: str) -> Any:
    """Parse the first JSON object in a model response (tolerates fences and think tags)."""
    cleaned = _THINK_RE.sub("", text or "").strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(cleaned[start:])
    return obj


def validate_output[M: BaseModel](raw: str, schema: type[M]) -> M:
    try:
        return schema.model_validate(extract_json_object(raw))
    except (ValueError, ValidationError) as exc:
        raise LLMOutputError(f"Model output failed validation for {schema.__name__}: {exc}") from exc


ScriptedResponse = str | dict[str, Any] | BaseModel | Callable[[str, str], Any] | Exception


@dataclass
class ScriptedLLM:
    """Returns queued responses per ``purpose``. Responses still go through validation."""

    responses: dict[str, list[ScriptedResponse]] = field(default_factory=dict)
    model_name: str = "scripted-demo-model"
    simulated: bool = True
    calls: list[dict[str, str]] = field(default_factory=list)
    budget_chars: int = 60_000
    max_validation_attempts: int = 2

    @property
    def input_budget_chars(self) -> int:
        return self.budget_chars

    def _next(self, purpose: str) -> ScriptedResponse:
        queue = self.responses.get(purpose)
        if not queue:
            raise LLMOutputError(f"No scripted response available for '{purpose}'.")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def complete_json(self, *, system: str, user: str, schema: type[T], purpose: str) -> LLMResult[T]:
        attempts = 0
        last_error: Exception | None = None
        while attempts < self.max_validation_attempts:
            attempts += 1
            self.calls.append({"purpose": purpose, "system": system, "user": user})
            response = self._next(purpose)
            if isinstance(response, Exception):
                raise response
            if callable(response) and not isinstance(response, BaseModel):
                response = response(system, user)
            if isinstance(response, BaseModel):
                raw = response.model_dump_json()
            elif isinstance(response, dict):
                raw = json.dumps(response)
            else:
                raw = str(response)
            try:
                data = validate_output(raw, schema)
                # No tokens are consumed by scripted responses; usage is reported as zero, never estimated.
                return LLMResult(data=data, model=self.model_name, attempts=attempts, simulated=True)
            except LLMOutputError as exc:
                last_error = exc
        raise LLMOutputError(str(last_error))
