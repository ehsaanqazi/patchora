"""Anthropic, OpenAI-compatible (OpenAI/DeepSeek/Gemini) and Bedrock clients with fake SDK clients.

No network and no credentials: each test injects a stand-in for the SDK client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import openai
import pytest
from botocore.exceptions import ClientError
from pydantic import BaseModel

from app.core.exceptions import ExternalAuthError, ExternalServiceError, LLMOutputError, NotConfiguredError
from app.providers.json_chat import ChatConfig, ContextTooLargeError
from app.services.anthropic_service import FALLBACK_BETA, AnthropicLLM
from app.services.bedrock_service import BedrockLLM
from app.services.openai_compatible_service import OpenAICompatibleLLM


class Out(BaseModel):
    answer: str


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.workers.retry_policy.asyncio.sleep", _sleep)


def _cfg(**overrides: Any) -> ChatConfig:
    values: dict[str, Any] = {"api_key": "test-key", "model": "some-model", "max_retries": 3}
    return ChatConfig(**(values | overrides))


def _http(status: int, url: str) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", url))


class Queue:
    """Async callable returning queued results (raising queued exceptions) and recording kwargs."""

    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# --- OpenAI-compatible -------------------------------------------------------------------------


def _chat(content: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                           usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4))


def _compat(provider: str, results: list[Any], **cfg: Any) -> tuple[OpenAICompatibleLLM, Queue]:
    create = Queue(results)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return OpenAICompatibleLLM(provider, _cfg(**cfg), client=client), create


async def test_openai_valid_output_uses_json_mode_and_no_temperature():
    llm, create = _compat("openai", [_chat('{"answer": "42"}')])
    result = await llm.complete_json(system="sys", user="question", schema=Out, purpose="test")
    assert result.data.answer == "42" and (result.prompt_tokens, result.completion_tokens) == (12, 4)
    call = create.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert "max_completion_tokens" in call and "temperature" not in call
    assert call["messages"][0]["role"] == "system" and "JSON schema" in call["messages"][0]["content"]


@pytest.mark.parametrize("provider", ["deepseek", "gemini"])
async def test_deepseek_and_gemini_send_max_tokens_and_temperature(provider):
    llm, create = _compat(provider, [_chat('{"answer": "ok"}')])
    await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert "max_tokens" in create.calls[0] and create.calls[0]["temperature"] == 0.1


async def test_compat_invalid_output_is_reasked_once():
    llm, create = _compat("openai", [_chat("not json"), _chat('{"answer": "fixed"}')])
    result = await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert result.data.answer == "fixed" and result.attempts == 2
    assert "did not match the schema" in create.calls[1]["messages"][-1]["content"]


async def test_compat_rate_limit_retried_auth_not():
    limited = openai.RateLimitError("slow", response=_http(429, "https://api.openai.com"), body=None)
    llm, create = _compat("openai", [limited, _chat('{"answer": "ok"}')])
    assert (await llm.complete_json(system="s", user="u", schema=Out, purpose="t")).data.answer == "ok"
    assert len(create.calls) == 2

    bad_key = openai.AuthenticationError("bad key", response=_http(401, "https://api.openai.com"), body=None)
    llm, create = _compat("openai", [bad_key])
    with pytest.raises(ExternalAuthError):
        await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert len(create.calls) == 1


async def test_compat_model_list_strips_gemini_prefix():
    async def models_list() -> Any:
        return SimpleNamespace(data=[SimpleNamespace(id="models/gemini-x"), SimpleNamespace(id="other")])

    llm, _ = _compat("gemini", [], model="gemini-x")
    llm._client.models = SimpleNamespace(list=models_list)  # type: ignore[attr-defined]
    details = await llm.diagnose()
    assert details["model_available"] is True and details["available_models"] == ["gemini-x", "other"]


def test_compat_requires_key_and_model():
    with pytest.raises(NotConfiguredError):
        OpenAICompatibleLLM("openai", _cfg(api_key=None), client=object())
    with pytest.raises(NotConfiguredError):
        OpenAICompatibleLLM("deepseek", _cfg(model=None), client=object())


async def test_input_budget_is_enforced_before_sending():
    llm, create = _compat("openai", [], max_input_tokens=10)
    with pytest.raises(ContextTooLargeError):
        await llm.complete_json(system="s" * 500, user="u", schema=Out, purpose="t")
    assert create.calls == []


# --- Anthropic ---------------------------------------------------------------------------------


def _message(parsed: BaseModel | None, stop: str = "end_turn") -> Any:
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop, content=[],
                           usage=SimpleNamespace(input_tokens=20, output_tokens=6, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0))


def _anthropic(results: list[Any], **cfg: Any) -> tuple[AnthropicLLM, Queue, Queue]:
    plain, beta = Queue(list(results)), Queue(list(results))
    client = SimpleNamespace(messages=SimpleNamespace(parse=plain),
                             beta=SimpleNamespace(messages=SimpleNamespace(parse=beta)))
    return AnthropicLLM(_cfg(**cfg), client=client), plain, beta


async def test_anthropic_structured_output_without_sampling_params():
    llm, plain, beta = _anthropic([_message(Out(answer="yes"))], model="claude-haiku-4-5")
    result = await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert result.data.answer == "yes" and (result.prompt_tokens, result.completion_tokens) == (20, 6)
    call = plain.calls[0]
    assert call["output_format"] is Out and "temperature" not in call and "thinking" not in call
    assert beta.calls == []  # no fallback beta for a model that does not support it


async def test_anthropic_fallback_enabled_for_supported_models_only():
    llm, plain, beta = _anthropic([_message(Out(answer="ok"))], model="claude-opus-5-5")
    await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert plain.calls == [] and beta.calls[0]["betas"] == [FALLBACK_BETA] and beta.calls[0]["fallbacks"] == "default"

    proxied, plain, beta = _anthropic([_message(Out(answer="ok"))], model="claude-opus-5-5",
                                      base_url="https://proxy.example")
    await proxied.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert beta.calls == [] and len(plain.calls) == 1


async def test_anthropic_refusal_is_never_parsed():
    llm, _, _ = _anthropic([_message(None, "refusal"), _message(None, "refusal")], model="claude-haiku-4-5")
    with pytest.raises(LLMOutputError, match=r"refusal|invalid"):
        await llm.complete_json(system="s", user="u", schema=Out, purpose="t")


async def test_anthropic_rate_limit_retried():
    limited = anthropic.RateLimitError("slow", response=_http(429, "https://api.anthropic.com"), body=None)
    llm, plain, _ = _anthropic([limited, _message(Out(answer="ok"))], model="claude-haiku-4-5")
    assert (await llm.complete_json(system="s", user="u", schema=Out, purpose="t")).data.answer == "ok"
    assert len(plain.calls) == 2


# --- AWS Bedrock -------------------------------------------------------------------------------


class FakeBedrock:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _converse(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": stop,
            "usage": {"inputTokens": 30, "outputTokens": 8}}


def _client_error(code: str, status: int) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
                       "Converse")


def _bedrock(results: list[Any]) -> tuple[BedrockLLM, FakeBedrock]:
    fake = FakeBedrock(results)
    return BedrockLLM(_cfg(api_key=None), region="us-east-1", client=fake), fake


async def test_bedrock_converse_output_and_usage():
    llm, fake = _bedrock([_converse('{"answer": "b"}')])
    result = await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert result.data.answer == "b" and (result.prompt_tokens, result.completion_tokens) == (30, 8)
    call = fake.calls[0]
    assert call["modelId"] == "some-model" and "JSON schema" in call["system"][0]["text"]
    assert "temperature" not in call["inferenceConfig"]


async def test_bedrock_throttling_retried_access_denied_not():
    llm, fake = _bedrock([_client_error("ThrottlingException", 429), _converse('{"answer": "ok"}')])
    assert (await llm.complete_json(system="s", user="u", schema=Out, purpose="t")).data.answer == "ok"
    assert len(fake.calls) == 2

    llm, fake = _bedrock([_client_error("AccessDeniedException", 403)])
    with pytest.raises(ExternalAuthError):
        await llm.complete_json(system="s", user="u", schema=Out, purpose="t")
    assert len(fake.calls) == 1


async def test_bedrock_truncated_output_is_an_error():
    llm, _ = _bedrock([_converse('{"ans', "max_tokens"), _converse('{"ans', "max_tokens")])
    with pytest.raises(LLMOutputError):
        await llm.complete_json(system="s", user="u", schema=Out, purpose="t")


def test_bedrock_configuration_checks():
    with pytest.raises(NotConfiguredError, match="region"):
        BedrockLLM(_cfg(api_key=None), region=None, client=object())
    with pytest.raises(NotConfiguredError, match="secret access key"):
        BedrockLLM(_cfg(api_key=None), region="us-east-1", access_key_id="AKIAEXAMPLE", client=object())
    # No key pair at all is allowed: the server's default AWS credential chain is used.
    assert BedrockLLM(_cfg(api_key=None), region="us-east-1", client=object()).region == "us-east-1"


def test_external_service_errors_keep_provider_name():
    error = ExternalServiceError("x", service="bedrock", retryable=True)
    assert error.service == "bedrock"
