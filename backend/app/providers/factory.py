"""Builds the external providers a run needs.

Real runs get Sentry, GitHub, the active LLM provider (Groq, Anthropic, OpenAI, DeepSeek, Gemini or
AWS Bedrock - runtime setting ``llm_provider``) and the configured sandbox. Demo runs get the demo
providers (no external calls, simulated results). Tests and the evaluation harness inject
their own providers through ``overrides``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import SandboxBackend, Settings
from app.core.exceptions import NotConfiguredError
from app.db.models import AnalysisRun, Repository
from app.providers.json_chat import ChatConfig, JSONChatLLM
from app.providers.llm import LLMClient
from app.sandbox.docker_runner import DockerSandbox
from app.sandbox.interface import Sandbox
from app.sandbox.local_runner import LocalSandbox
from app.services.anthropic_service import AnthropicLLM
from app.services.bedrock_service import BedrockLLM
from app.services.credential_service import all_secret_values, credential_value
from app.services.github_service import (
    CodeHost,
    GitHubAppAuth,
    GitHubService,
    PersonalAccessTokenAuth,
    TokenProvider,
)
from app.services.groq_service import GroqConfig, GroqLLM
from app.services.openai_compatible_service import OpenAICompatibleLLM
from app.services.search_service import SemanticIndex, faiss_available
from app.services.sentry_service import IssueSource, SentryService
from app.services.settings_service import RuntimeSettings, get_runtime_settings


class _DisabledSandbox:
    name = "disabled"
    isolation = "none"
    simulated = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def available(self) -> tuple[bool, str]:
        return False, self.reason

    async def run(self, workspace: Path, argv: list[str], limits: Any, *, image: str | None = None) -> Any:
        from app.sandbox.result import ExecutionResult

        return ExecutionResult(exit_code=None, stdout="", stderr="", duration_ms=0, backend=self.name,
                               error=self.reason)


@dataclass
class RunDependencies:
    issue_source: IssueSource | None
    issue_source_error: str | None
    code_host: CodeHost | None
    code_host_error: str | None
    llm: LLMClient | None
    llm_error: str | None
    sandbox: Sandbox
    runtime: RuntimeSettings
    settings: Settings
    known_secrets: list[str]
    workspace_root: Path
    semantic_index: SemanticIndex | None = None
    closers: list[Callable[[], Any]] = field(default_factory=list)

    def require_issue_source(self) -> IssueSource:
        if self.issue_source is None:
            raise NotConfiguredError(self.issue_source_error or "Sentry is not configured.")
        return self.issue_source

    def require_code_host(self) -> CodeHost:
        if self.code_host is None:
            raise NotConfiguredError(self.code_host_error or "GitHub is not configured.")
        return self.code_host

    def require_llm(self) -> LLMClient:
        if self.llm is None:
            raise NotConfiguredError(self.llm_error or "No LLM provider is configured.")
        return self.llm

    async def aclose(self) -> None:
        for closer in self.closers:
            result = closer()
            if hasattr(result, "__await__"):
                await result


class ProviderFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # name -> callable(session, run) returning a provider; used by tests and evaluation.
        self.overrides: dict[str, Callable[[Session, AnalysisRun | None], Any]] = {}
        self._app_auth: dict[str, GitHubAppAuth] = {}

    # --- individual providers ------------------------------------------------------------

    def sentry(self, session: Session) -> SentryService:
        token = credential_value(session, "sentry", "auth_token", self.settings)
        org = credential_value(session, "sentry", "organization", self.settings)
        base_url = credential_value(session, "sentry", "base_url", self.settings) or "https://sentry.io"
        return SentryService(base_url=base_url, token=token, organization=org,
                             timeout=self.settings.sentry_timeout_seconds)

    def github_auth(self, session: Session, repository: Repository | None = None) -> TokenProvider:
        s = self.settings
        app_id = credential_value(session, "github", "app_id", s)
        installation = (repository.installation_id if repository else None) or self.github_installation_id(session)
        key = credential_value(session, "github", "private_key", s)
        if app_id and key and installation:
            cache_key = f"{app_id}:{installation}:{hash(key)}"  # a changed key/app id builds a new client
            if cache_key not in self._app_auth:
                self._app_auth[cache_key] = GitHubAppAuth(app_id, key, installation)
            return self._app_auth[cache_key]
        pat = credential_value(session, "github", "personal_access_token", s)
        if pat:
            return PersonalAccessTokenAuth(pat)
        if app_id:
            raise NotConfiguredError("GitHub App is incomplete: set the installation id and private key on the "
                                     "Integrations page.")
        raise NotConfiguredError("GitHub is not configured. Add GitHub App details or a personal access token "
                                 "on the Integrations page.")

    def github_installation_id(self, session: Session) -> str | None:
        return credential_value(session, "github", "installation_id", self.settings)

    def github(self, session: Session, repository: Repository | None = None) -> GitHubService:
        api_url = credential_value(session, "github", "api_url", self.settings) or "https://api.github.com"
        return GitHubService(self.github_auth(session, repository), api_url=api_url,
                             timeout=self.settings.github_timeout_seconds,
                             max_file_bytes=self.settings.github_max_file_bytes,
                             max_tarball_bytes=self.settings.github_max_tarball_bytes)

    def groq(self, session: Session, runtime: RuntimeSettings) -> GroqLLM:
        s = self.settings
        return GroqLLM(GroqConfig(
            api_key=credential_value(session, "groq", "api_key", s),
            model=credential_value(session, "groq", "model", s),
            base_url=credential_value(session, "groq", "base_url", s),
            timeout_seconds=s.groq_timeout_seconds, max_retries=s.groq_max_retries,
            max_input_tokens=s.groq_max_input_tokens, max_output_tokens=s.groq_max_output_tokens,
            structured_output_mode=s.groq_structured_output_mode, temperature=s.groq_temperature,
        ))

    def _chat_config(self, session: Session, provider: str) -> ChatConfig:
        s = self.settings
        api_key = credential_value(session, provider, "api_key", s) if provider != "bedrock" else None
        return ChatConfig(
            api_key=api_key,
            model=credential_value(session, provider, "model", s),
            base_url=credential_value(session, provider, "base_url", s) if provider != "bedrock" else None,
            timeout_seconds=s.llm_timeout_seconds, max_retries=s.llm_max_retries,
            max_input_tokens=s.llm_max_input_tokens, max_output_tokens=s.llm_max_output_tokens,
            temperature=s.llm_temperature,
        )

    def llm_for(self, session: Session, provider: str) -> GroqLLM | JSONChatLLM:
        """Build the client for one specific provider (used by connection tests and model lists)."""
        if provider == "groq":
            return self.groq(session, get_runtime_settings(session, self.settings))
        if provider == "anthropic":
            return AnthropicLLM(self._chat_config(session, provider))
        if provider in {"openai", "deepseek", "gemini"}:
            return OpenAICompatibleLLM(provider, self._chat_config(session, provider))
        if provider == "bedrock":
            s = self.settings
            return BedrockLLM(
                self._chat_config(session, provider),
                region=credential_value(session, "bedrock", "region", s),
                access_key_id=credential_value(session, "bedrock", "access_key_id", s),
                secret_access_key=credential_value(session, "bedrock", "secret_access_key", s),
                session_token=credential_value(session, "bedrock", "session_token", s),
            )
        raise NotConfiguredError(f"Unknown LLM provider '{provider}'.")

    def llm(self, session: Session, runtime: RuntimeSettings) -> GroqLLM | JSONChatLLM:
        """The active LLM provider (chosen on the Integrations page)."""
        return self.llm_for(session, runtime.llm_provider.value)

    def sandbox(self, runtime: RuntimeSettings) -> Sandbox:
        if not self.settings.sandbox_enabled or runtime.sandbox_backend == SandboxBackend.DISABLED:
            return _DisabledSandbox("Sandboxed execution is disabled by configuration.")
        if runtime.sandbox_backend == SandboxBackend.LOCAL:
            allowed = runtime.allow_unsafe_local_execution and self.settings.app_env == "development"
            return LocalSandbox(enabled=allowed)
        return DockerSandbox(self.settings.sandbox_python_image)

    # --- per-run bundle --------------------------------------------------------------------

    def for_run(self, session: Session, run: AnalysisRun) -> RunDependencies:
        runtime = get_runtime_settings(session, self.settings)
        workspace_root = self.settings.data_dir / "workspaces"
        closers: list[Callable[[], Any]] = []

        def _try(name: str, build: Callable[[], Any]) -> tuple[Any, str | None]:
            if name in self.overrides:
                return self.overrides[name](session, run), None
            try:
                provider = build()
            except NotConfiguredError as exc:
                return None, exc.message
            if hasattr(provider, "aclose"):
                closers.append(provider.aclose)
            return provider, None

        state = run.state_json or {}
        if run.is_demo:
            from app.demo.providers import DemoCodeHost, DemoIssueSource, demo_llm, demo_sandbox

            scenario = state.get("demo_scenario")
            issue_source, issue_err = _try("issue_source", DemoIssueSource)
            code_host, host_err = _try("code_host", DemoCodeHost)
            if state.get("use_real_llm"):
                llm, llm_err = _try("llm", lambda: self.llm(session, runtime))
                sandbox = self.overrides["sandbox"](session, run) if "sandbox" in self.overrides \
                    else self.sandbox(runtime)
            else:
                llm, llm_err = _try("llm", lambda: demo_llm(scenario))
                sandbox = self.overrides["sandbox"](session, run) if "sandbox" in self.overrides \
                    else demo_sandbox(scenario)
        else:
            repository = session.get(Repository, run.repository_id) if run.repository_id else None
            issue_source, issue_err = _try("issue_source", lambda: self.sentry(session))
            code_host, host_err = _try("code_host", lambda: self.github(session, repository))
            llm, llm_err = _try("llm", lambda: self.llm(session, runtime))
            sandbox = self.overrides["sandbox"](session, run) if "sandbox" in self.overrides \
                else self.sandbox(runtime)

        semantic = None
        if runtime.faiss_enabled and faiss_available()[0]:
            semantic = SemanticIndex(self.settings.faiss_index_path, self.settings.embedding_model)

        return RunDependencies(
            issue_source=issue_source, issue_source_error=issue_err,
            code_host=code_host, code_host_error=host_err,
            llm=llm, llm_error=llm_err, sandbox=sandbox, runtime=runtime, settings=self.settings,
            known_secrets=all_secret_values(session, self.settings), workspace_root=workspace_root,
            semantic_index=semantic, closers=closers,
        )
