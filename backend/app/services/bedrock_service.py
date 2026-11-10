"""AWS Bedrock inference through the Converse API (``boto3``), for any Bedrock chat model.

* Credentials: an access key pair saved on the Integrations page, or - when none is saved - the
  server's default AWS credential chain (IAM role, ``AWS_PROFILE``, environment).
* The model id may be a foundation model id or an inference profile id/ARN.
* boto3 is synchronous, so each call runs in a worker thread. Its own retries are disabled; the shared
  bounded policy retries only throttling, timeouts and 5xx-type failures.
* No sampling parameters are sent (some current models reject them).
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel

from app.core.exceptions import (
    ExternalAuthError,
    ExternalNotFound,
    ExternalServiceError,
    LLMOutputError,
    NotConfiguredError,
)
from app.providers.json_chat import ChatConfig, ChatReply, ContextTooLargeError, JSONChatLLM

_RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "InternalServerException",
              "ModelTimeoutException", "ModelNotReadyException", "TooManyRequestsException"}
_AUTH = {"AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException",
         "InvalidSignatureException", "UnauthorizedException"}


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, NoCredentialsError):
        return NotConfiguredError("No AWS credentials found. Add an access key on the Integrations page or "
                                  "give the server an IAM role/profile.")
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", ""))
        message = str(error.get("Message", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in _RETRYABLE:
            return ExternalServiceError(f"Bedrock is busy or unavailable ({code})", service="bedrock",
                                        retryable=True, upstream_status=status)
        if code in _AUTH:
            return ExternalAuthError(f"AWS rejected the credentials or the model is not enabled for them ({code})",
                                     service="bedrock", upstream_status=status or 403)
        if code == "ResourceNotFoundException":
            return ExternalNotFound("The configured Bedrock model was not found in this region",
                                    service="bedrock", upstream_status=404)
        if code == "ValidationException":
            lowered = message.lower()
            if "too long" in lowered or "context" in lowered or "too many tokens" in lowered \
                    or "input is too long" in lowered:
                return ContextTooLargeError("Prompt exceeds the model's context window", service="bedrock")
            return ExternalServiceError(f"Bedrock rejected the request: {message[:200]}", service="bedrock",
                                        upstream_status=400)
        return ExternalServiceError(f"Bedrock error {code}: {message[:200]}", service="bedrock",
                                    retryable=bool(status and status >= 500), upstream_status=status)
    if isinstance(exc, BotoCoreError):  # connection errors, read/connect timeouts
        return ExternalServiceError(f"Could not reach Bedrock: {type(exc).__name__}", service="bedrock",
                                    retryable=True)
    return exc


class BedrockLLM(JSONChatLLM):
    provider = "bedrock"
    label = "AWS Bedrock"
    requires_api_key = False  # the default AWS credential chain may be used

    def __init__(self, config: ChatConfig, *, region: str | None, access_key_id: str | None = None,
                 secret_access_key: str | None = None, session_token: str | None = None,
                 client: Any | None = None) -> None:
        if not region:
            raise NotConfiguredError("AWS Bedrock region is not configured. Set it on the Integrations page.")
        if access_key_id and not secret_access_key:
            raise NotConfiguredError("AWS Bedrock: an access key id is set but the secret access key is missing.")
        super().__init__(config)
        self.region = region
        if client is None:
            session = boto3.session.Session(
                aws_access_key_id=access_key_id or None,
                aws_secret_access_key=secret_access_key or None,
                aws_session_token=session_token or None,
                region_name=region,
            )
            client = session.client("bedrock-runtime", config=Config(
                read_timeout=config.timeout_seconds, connect_timeout=10,
                retries={"max_attempts": 0}))  # retries are handled by our bounded policy
        self._client = client

    def _converse(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
        return self._client.converse(
            modelId=self.model_name,
            system=[{"text": system}],
            messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
            inferenceConfig={"maxTokens": max_tokens},
        )

    async def _call(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._converse, system, messages, max_tokens)
        except (ClientError, BotoCoreError) as exc:
            raise _map_error(exc) from exc

    async def _send(self, system: str, messages: list[dict[str, str]], schema: type[BaseModel]) -> ChatReply:
        response = await self._call(system, messages, self.config.max_output_tokens)
        stop = response.get("stopReason")
        if stop in {"content_filtered", "guardrail_intervened"}:
            raise LLMOutputError(f"Bedrock blocked the output (stopReason: {stop})")
        if stop == "max_tokens":
            raise LLMOutputError("Bedrock output was cut off at max_tokens; raise LLM_MAX_OUTPUT_TOKENS")
        content = response.get("output", {}).get("message", {}).get("content", []) or []
        text = "".join(block.get("text", "") for block in content if isinstance(block, dict))
        usage = response.get("usage", {}) or {}
        return ChatReply(text=text, prompt_tokens=int(usage.get("inputTokens", 0) or 0),
                         completion_tokens=int(usage.get("outputTokens", 0) or 0))

    async def diagnose(self) -> dict[str, Any]:
        """Bedrock has no per-key model list, so send one tiny request to verify credentials and access."""
        await self._call("Reply with the JSON object {\"ok\": true} and nothing else.",
                         [{"role": "user", "content": "ping"}], 32)
        return {"key_valid": True, "model": self.model_name, "model_available": True, "region": self.region,
                "provider": self.label, "available_models": []}
