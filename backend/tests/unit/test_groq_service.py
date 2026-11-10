"""Groq client behaviour with a fake SDK client (no network, no credentials)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
from pydantic import BaseModel

from app.core.exceptions import ExternalAuthError, LLMOutputError, NotConfiguredError
from app.services.groq_service import ContextTooLargeError, GroqConfig, GroqLLM, estimate_cost


class Out(BaseModel):
    answer: str


def _completion(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "https://api.groq.com"))


class FakeCompletions:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _llm(results: list[Any], **config: Any) -> tuple[GroqLLM, FakeCompletions]:
    completions = FakeCompletions(results)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    cfg = GroqConfig(api_key="gsk_test", model="some-model", max_retries=3, **config)
    return GroqLLM(cfg, client=client), completions  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.workers.retry_policy.asyncio.sleep", _sleep)


async def test_valid_output_and_usage():
    llm, calls = _llm([_completion('{"answer": "42"}')])
    result = await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert result.data.answer == "42" and result.prompt_tokens == 10 and not result.simulated
    assert calls.calls[0]["response_format"] == {"type": "json_object"}
    assert calls.calls[0]["model"] == "some-model"


async def test_invalid_output_is_reasked_once():
    llm, calls = _llm([_completion("not json"), _completion('```json\n{"answer": "ok"}\n```')])
    result = await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert result.data.answer == "ok" and result.attempts == 2
    assert "did not match the schema" in calls.calls[1]["messages"][-1]["content"]


async def test_invalid_output_twice_fails():
    llm, _ = _llm([_completion('{"wrong": 1}'), _completion('{"still": "wrong"}')])
    with pytest.raises(LLMOutputError):
        await llm.complete_json(system="s", user="u", schema=Out, purpose="t")


async def test_rate_limit_is_retried():
    rate_limited = groq.RateLimitError("slow down", response=_response(429, {"retry-after": "0"}), body=None)
    llm, calls = _llm([rate_limited, _completion('{"answer": "a"}')])
    result = await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert result.data.answer == "a" and len(calls.calls) == 2


async def test_auth_errors_are_not_retried():
    error = groq.AuthenticationError("bad key", response=_response(401), body=None)
    llm, calls = _llm([error, _completion('{"answer": "a"}')])
    with pytest.raises(ExternalAuthError):
        await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert len(calls.calls) == 1


async def test_json_validate_failed_maps_to_output_error_then_recovers():
    error = groq.BadRequestError("json_validate_failed: Failed to generate JSON", response=_response(400), body=None)
    llm, _ = _llm([error, _completion('{"answer": "b"}')])
    result = await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert result.data.answer == "b"


async def test_context_budget_enforced():
    llm, calls = _llm([_completion("{}")], max_input_tokens=100)
    with pytest.raises(ContextTooLargeError):
        await llm.complete_json(system="s", user="x" * 5000, schema=Out, purpose="t")
    assert calls.calls == []


def test_configuration_required():
    with pytest.raises(NotConfiguredError):
        GroqLLM(GroqConfig(api_key=None, model="m"))
    with pytest.raises(NotConfiguredError, match="GROQ_MODEL"):
        GroqLLM(GroqConfig(api_key="k", model=None))


def test_cost_only_with_configured_pricing():
    assert estimate_cost(1000, 1000, None, None) is None
    assert estimate_cost(1_000_000, 0, 0.5, 1.0) == 0.5
