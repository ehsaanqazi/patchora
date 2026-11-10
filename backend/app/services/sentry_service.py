"""Sentry REST API client and payload parsers.

Endpoints used (documented Sentry web API):

* ``GET /api/0/projects/{org}/{project}/issues/``            - list a project's issues
* ``GET /api/0/organizations/{org}/issues/{issue_id}/``      - retrieve an issue
* ``GET /api/0/organizations/{org}/issues/{issue_id}/events/latest/`` - latest event
* ``GET /api/0/organizations/{org}/releases/{version}/commits/``      - release commits
* ``GET /api/0/organizations/{org}/``                         - connection test

Only read access (``event:read``, ``project:read``, ``org:read``) is required.
Parsers never invent data: missing frames, lines or releases stay missing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.exceptions import ExternalServiceError, NotConfiguredError
from app.schemas.issues import (
    ExceptionInfo,
    NormalizedEvent,
    NormalizedIssue,
    ReleaseCommit,
    StackFrame,
)
from app.security.input_sanitization import truncate, validate_sentry_issue_id, validate_slug
from app.services import http
from app.services.http import parse_link_header
from app.workers.retry_policy import RetryPolicy

MAX_FRAMES = 60
MAX_CONTEXT_LINE = 500


class IssueSource(Protocol):
    organization: str
    simulated: bool

    async def list_issues(self, project: str, *, query: str | None = None, cursor: str | None = None,
                          limit: int = 25) -> tuple[list[NormalizedIssue], str | None]: ...
    async def get_issue(self, issue_id: str) -> NormalizedIssue: ...
    async def get_latest_event(self, issue_id: str) -> NormalizedEvent: ...
    async def get_release_commits(self, version: str) -> list[ReleaseCommit]: ...


def _dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_issue(data: dict[str, Any], organization: str) -> NormalizedIssue:
    if not isinstance(data, dict) or "id" not in data:
        raise ExternalServiceError("Unexpected Sentry issue payload", service="sentry")
    metadata = data.get("metadata") or {}
    project = data.get("project") or {}
    return NormalizedIssue(
        sentry_issue_id=str(data["id"]),
        short_id=data.get("shortId"),
        organization=organization,
        project=str(project.get("slug") or project.get("name") or "unknown"),
        title=truncate(str(data.get("title") or metadata.get("title") or "(untitled)"), 500),
        culprit=truncate(data.get("culprit"), 500) or None,
        exception_type=metadata.get("type"),
        level=data.get("level"),
        status=data.get("status"),
        permalink=data.get("permalink"),
        first_seen=_dt(data.get("firstSeen")),
        last_seen=_dt(data.get("lastSeen")),
        event_count=_int(data.get("count")),
        metadata={k: metadata.get(k) for k in ("type", "value", "filename", "function") if k in metadata},
    )


def _parse_frame(frame: dict[str, Any]) -> StackFrame:
    context: list[tuple[int, str]] = []
    for item in frame.get("context") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], int):
            context.append((item[0], truncate(str(item[1]), MAX_CONTEXT_LINE)))
    return StackFrame(
        filename=frame.get("filename"),
        abs_path=frame.get("absPath") or frame.get("abs_path"),
        module=frame.get("module"),
        function=frame.get("function"),
        lineno=frame.get("lineNo") if frame.get("lineNo") is not None else frame.get("lineno"),
        colno=frame.get("colNo") if frame.get("colNo") is not None else frame.get("colno"),
        in_app=frame.get("inApp") if "inApp" in frame else frame.get("in_app"),
        context=context[:15],
    )


def _parse_exception_values(values: list[Any]) -> list[ExceptionInfo]:
    result: list[ExceptionInfo] = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        frames_raw = ((value.get("stacktrace") or {}).get("frames")) or []
        frames = [_parse_frame(f) for f in frames_raw if isinstance(f, dict)]
        mechanism = value.get("mechanism") or {}
        result.append(ExceptionInfo(
            type=value.get("type"),
            value=truncate(value.get("value"), 2000) or None,
            module=value.get("module"),
            mechanism=mechanism.get("type") if isinstance(mechanism, dict) else None,
            frames=frames[-MAX_FRAMES:],
        ))
    return result


def parse_event(data: dict[str, Any]) -> NormalizedEvent:
    if not isinstance(data, dict):
        raise ExternalServiceError("Unexpected Sentry event payload", service="sentry")
    exceptions: list[ExceptionInfo] = []
    for entry in data.get("entries") or []:
        if isinstance(entry, dict) and entry.get("type") == "exception":
            exceptions.extend(_parse_exception_values((entry.get("data") or {}).get("values") or []))
    if not exceptions and isinstance(data.get("exception"), dict):  # raw event format
        exceptions = _parse_exception_values(data["exception"].get("values") or [])
    tags: dict[str, str] = {}
    for tag in data.get("tags") or []:
        if isinstance(tag, dict) and "key" in tag:
            tags[str(tag["key"])] = truncate(str(tag.get("value")), 200)
        elif isinstance(tag, (list, tuple)) and len(tag) == 2:
            tags[str(tag[0])] = truncate(str(tag[1]), 200)
    release = data.get("release")
    if isinstance(release, dict):
        release = release.get("version")
    release = release or tags.get("release")
    return NormalizedEvent(
        event_id=str(data.get("eventID") or data.get("event_id") or data.get("id") or ""),
        title=truncate(data.get("title"), 500) or None,
        message=truncate(data.get("message"), 2000) or None,
        platform=data.get("platform"),
        release=str(release) if release else None,
        environment=tags.get("environment") or data.get("environment"),
        date_created=_dt(data.get("dateCreated")),
        exceptions=exceptions,
        tags=tags,
    )


class SentryService:
    simulated = False

    def __init__(self, *, base_url: str, token: str | None, organization: str | None,
                 timeout: float = 30.0, client: httpx.AsyncClient | None = None) -> None:
        if not token:
            raise NotConfiguredError("SENTRY_AUTH_TOKEN is not configured.")
        if not organization:
            raise NotConfiguredError("SENTRY_ORG is not configured.")
        self.organization = validate_slug(organization, "Sentry organization")
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._policy = RetryPolicy(max_attempts=3, base_delay=1.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return await http.request(self._client, "sentry", "GET", f"{self._base}{path}",
                                  headers=self._headers, params=params, policy=self._policy)

    async def test_connection(self) -> dict[str, Any]:
        response = await self._get(f"/api/0/organizations/{self.organization}/")
        body = response.json()
        return {"organization": body.get("slug"), "name": body.get("name")}

    async def list_issues(self, project: str, *, query: str | None = None, cursor: str | None = None,
                          limit: int = 25) -> tuple[list[NormalizedIssue], str | None]:
        validate_slug(project, "Sentry project")
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if query:
            params["query"] = query[:500]
        if cursor:
            params["cursor"] = cursor[:200]
        response = await self._get(f"/api/0/projects/{self.organization}/{project}/issues/", params)
        body = response.json()
        if not isinstance(body, list):
            raise ExternalServiceError("Unexpected Sentry issue list payload", service="sentry")
        issues = [parse_issue(item, self.organization) for item in body if isinstance(item, dict)]
        nxt = parse_link_header(response.headers.get("link")).get("next")
        next_cursor = nxt.get("cursor") if nxt and nxt.get("results") == "true" else None
        return issues, next_cursor

    async def get_issue(self, issue_id: str) -> NormalizedIssue:
        validate_sentry_issue_id(issue_id)
        response = await self._get(f"/api/0/organizations/{self.organization}/issues/{issue_id}/")
        return parse_issue(response.json(), self.organization)

    async def get_latest_event(self, issue_id: str) -> NormalizedEvent:
        validate_sentry_issue_id(issue_id)
        response = await self._get(
            f"/api/0/organizations/{self.organization}/issues/{issue_id}/events/latest/"
        )
        return parse_event(response.json())

    async def get_release_commits(self, version: str) -> list[ReleaseCommit]:
        if not version or len(version) > 250:
            return []
        response = await self._get(
            f"/api/0/organizations/{self.organization}/releases/{quote(version, safe='')}/commits/"
        )
        body = response.json()
        commits: list[ReleaseCommit] = []
        for item in body if isinstance(body, list) else []:
            if isinstance(item, dict) and item.get("id"):
                repo = item.get("repository") or {}
                commits.append(ReleaseCommit(sha=str(item["id"]), repository=repo.get("name"),
                                             message=truncate(item.get("message"), 300)))
        return commits
