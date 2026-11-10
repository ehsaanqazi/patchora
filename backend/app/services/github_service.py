"""GitHub REST API client.

Authentication:
* GitHub App installation tokens (preferred): a short-lived RS256 JWT signed with the App's
  private key is exchanged for an installation access token, cached until shortly before it
  expires.
* A narrowly scoped personal access token for local development.

Write safety (enforced structurally):
* The only ref-writing call is ``POST /git/refs`` (create). There is no ref update/force-push
  code path, so an existing branch - including the default branch - can never be overwritten.
* Branches must start with ``patchora/`` and pass git ref-format validation.
* Nothing here merges pull requests or triggers deployments.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote

import anyio
import httpx
import jwt

from app.core.exceptions import (
    ConflictError,
    ExternalNotFound,
    ExternalServiceError,
    NotConfiguredError,
    PolicyViolation,
)
from app.core.logging import get_logger
from app.schemas.repositories import (
    CommitInfo,
    FileContent,
    IssueInfo,
    PullRequestInfo,
    RepoInfo,
    TreeEntry,
)
from app.security.input_sanitization import (
    is_valid_sha,
    normalize_repo_path,
    validate_branch_name,
    validate_full_name,
)
from app.services import http
from app.services.http import parse_link_header
from app.workers.retry_policy import RetryPolicy

logger = get_logger(__name__)

BRANCH_PREFIX = "patchora/"
API_VERSION = "2022-11-28"


class CodeHost(Protocol):
    simulated: bool

    async def get_repository(self, full_name: str) -> RepoInfo: ...
    async def list_repositories(self) -> list[RepoInfo]: ...
    async def get_commit(self, full_name: str, ref: str) -> CommitInfo: ...
    async def get_branch_head(self, full_name: str, branch: str) -> str | None: ...
    async def get_tree(self, full_name: str, sha: str) -> list[TreeEntry]: ...
    async def get_file(self, full_name: str, path: str, ref: str) -> FileContent: ...
    async def download_archive(self, full_name: str, sha: str, dest: Path) -> Path: ...
    async def changed_files_between(self, full_name: str, base: str, head: str) -> list[str]: ...
    async def create_branch_with_commit(
        self, full_name: str, *, base_sha: str, branch: str, default_branch: str,
        files: dict[str, str], file_modes: dict[str, str], message: str,
    ) -> str: ...
    async def find_pull_request(self, full_name: str, branch: str) -> PullRequestInfo | None: ...
    async def create_pull_request(self, full_name: str, *, title: str, body: str, head: str, base: str,
                                  draft: bool) -> PullRequestInfo: ...
    async def get_pull_request(self, full_name: str, number: int) -> PullRequestInfo: ...
    async def find_issue_by_marker(self, full_name: str, marker: str) -> IssueInfo | None: ...
    async def create_issue(self, full_name: str, *, title: str, body: str,
                           labels: list[str]) -> IssueInfo: ...


# --- Authentication -----------------------------------------------------------------------

class TokenProvider(Protocol):
    kind: str

    async def token(self, client: httpx.AsyncClient, api_url: str) -> str: ...


@dataclass
class PersonalAccessTokenAuth:
    value: str
    kind: str = "personal_access_token"

    async def token(self, client: httpx.AsyncClient, api_url: str) -> str:
        return self.value


class GitHubAppAuth:
    kind = "github_app"

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str) -> None:
        if not app_id or not private_key_pem or not installation_id:
            raise NotConfiguredError("GitHub App credentials are incomplete.")
        self.app_id = app_id
        self._private_key = private_key_pem
        self.installation_id = installation_id
        self._cached: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    def app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def token(self, client: httpx.AsyncClient, api_url: str) -> str:
        async with self._lock:
            if self._cached and self._cached[1] - 60 > time.time():
                return self._cached[0]
            response = await http.request(
                client, "github", "POST",
                f"{api_url}/app/installations/{quote(self.installation_id)}/access_tokens",
                headers={"Authorization": f"Bearer {self.app_jwt()}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": API_VERSION},
            )
            body = response.json()
            token = body.get("token")
            if not token:
                raise ExternalServiceError("GitHub did not return an installation token", service="github")
            expires_at = time.time() + 3000
            if body.get("expires_at"):
                from datetime import datetime

                try:
                    expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            self._cached = (token, expires_at)
            return token


def load_private_key(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# --- Archive extraction -------------------------------------------------------------------

def safe_extract_tarball(archive: Path, dest: Path, max_files: int = 50_000,
                         max_total_bytes: int = 1_000_000_000) -> list[str]:
    """Extract a GitHub tarball, stripping the top-level directory.

    Rejects absolute paths, traversal, links and special files. Symlinks are skipped (and
    reported) so nothing inside the workspace can point outside it.
    """
    skipped: list[str] = []
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    count = 0
    with tarfile.open(archive, mode="r:*") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if len(parts) <= 1:
                continue
            relative = "/".join(parts[1:])
            try:
                relative = normalize_repo_path(relative)
            except Exception:
                skipped.append(member.name)
                continue
            target = (dest / relative).resolve()
            if not target.is_relative_to(dest):
                skipped.append(member.name)
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                skipped.append(relative)  # symlinks, hardlinks, devices, fifos
                continue
            count += 1
            total += member.size
            if count > max_files or total > max_total_bytes:
                raise PolicyViolation("Repository archive exceeds extraction limits.")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return skipped


# --- Client -------------------------------------------------------------------------------

class GitHubService:
    simulated = False

    def __init__(self, auth: TokenProvider, *, api_url: str = "https://api.github.com",
                 timeout: float = 30.0, max_file_bytes: int = 300_000,
                 max_tarball_bytes: int = 200_000_000, client: httpx.AsyncClient | None = None) -> None:
        self.auth = auth
        self.api_url = api_url.rstrip("/")
        self.max_file_bytes = max_file_bytes
        self.max_tarball_bytes = max_tarball_bytes
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._policy = RetryPolicy(max_attempts=3, base_delay=1.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _headers(self) -> dict[str, str]:
        token = await self.auth.token(self._client, self.api_url)
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION}

    async def _req(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                   json: Any = None, expected_errors: tuple[int, ...] = (),
                   retry: bool = True) -> httpx.Response:
        policy = self._policy if retry else RetryPolicy(max_attempts=1)
        return await http.request(self._client, "github", method, f"{self.api_url}{path}",
                                  headers=await self._headers(), params=params, json=json,
                                  policy=policy, expected_errors=expected_errors)

    @staticmethod
    def _repo_path(full_name: str) -> str:
        owner, name = validate_full_name(full_name)
        return f"/repos/{owner}/{name}"

    @staticmethod
    def _repo_info(body: dict[str, Any]) -> RepoInfo:
        return RepoInfo(
            provider_repository_id=str(body["id"]),
            owner=body["owner"]["login"],
            name=body["name"],
            full_name=body["full_name"],
            default_branch=body.get("default_branch") or "main",
            private=bool(body.get("private", True)),
            permissions={k: bool(v) for k, v in (body.get("permissions") or {}).items()},
        )

    # --- read operations -----------------------------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        if self.auth.kind == "github_app":
            response = await self._req("GET", "/installation/repositories", params={"per_page": 1})
            return {"auth": "github_app", "repositories_visible": response.json().get("total_count")}
        response = await self._req("GET", "/user")
        return {"auth": "personal_access_token", "login": response.json().get("login")}

    async def get_repository(self, full_name: str) -> RepoInfo:
        response = await self._req("GET", self._repo_path(full_name))
        return self._repo_info(response.json())

    async def list_repositories(self) -> list[RepoInfo]:
        repos: list[RepoInfo] = []
        params: dict[str, Any]
        if self.auth.kind == "github_app":
            path, params = "/installation/repositories", {"per_page": 100, "page": 1}
        else:
            path, params = "/user/repos", {"per_page": 100, "page": 1,
                                           "affiliation": "owner,collaborator,organization_member"}
        for _ in range(10):  # at most 1000 repositories
            response = await self._req("GET", path, params=params)
            body = response.json()
            items = body.get("repositories", []) if isinstance(body, dict) else body
            repos.extend(self._repo_info(item) for item in items)
            if "next" not in parse_link_header(response.headers.get("link")):
                break
            params["page"] += 1
        return repos

    async def get_commit(self, full_name: str, ref: str) -> CommitInfo:
        if not (is_valid_sha(ref) or ref):
            raise ExternalNotFound("Invalid ref", service="github")
        response = await self._req("GET", f"{self._repo_path(full_name)}/commits/{quote(ref, safe='')}")
        body = response.json()
        return CommitInfo(sha=body["sha"], tree_sha=body["commit"]["tree"]["sha"],
                          message=(body["commit"].get("message") or "")[:2000],
                          date=(body["commit"].get("committer") or {}).get("date"))

    async def get_branch_head(self, full_name: str, branch: str) -> str | None:
        validate_branch_name(branch)
        response = await self._req("GET", f"{self._repo_path(full_name)}/git/ref/heads/{quote(branch, safe='/')}",
                                   expected_errors=(404,))
        if response.status_code == 404:
            return None
        return str(response.json()["object"]["sha"])

    async def get_tree(self, full_name: str, sha: str) -> list[TreeEntry]:
        response = await self._req("GET", f"{self._repo_path(full_name)}/git/trees/{quote(sha, safe='')}",
                                   params={"recursive": "1"})
        body = response.json()
        entries = [TreeEntry(path=e["path"], type=e["type"], mode=e["mode"], size=e.get("size"),
                             sha=e.get("sha")) for e in body.get("tree", [])]
        if body.get("truncated"):
            logger.warning("github tree truncated", extra={"repo": full_name})
        return entries

    async def get_file(self, full_name: str, path: str, ref: str) -> FileContent:
        path = normalize_repo_path(path)
        response = await self._req("GET", f"{self._repo_path(full_name)}/contents/{quote(path, safe='/')}",
                                   params={"ref": ref})
        body = response.json()
        if isinstance(body, list):
            raise ExternalServiceError(f"{path} is a directory", service="github")
        size = int(body.get("size") or 0)
        if body.get("type") == "symlink":
            return FileContent(path=path, symlink=True, size=size, sha=body.get("sha"))
        if body.get("type") != "file":
            return FileContent(path=path, binary=True, size=size, sha=body.get("sha"))
        if size > self.max_file_bytes:
            return FileContent(path=path, too_large=True, size=size, sha=body.get("sha"))
        raw = base64.b64decode(body.get("content") or "") if body.get("encoding") == "base64" else b""
        if b"\x00" in raw[:8000]:
            return FileContent(path=path, binary=True, size=size, sha=body.get("sha"))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return FileContent(path=path, binary=True, size=size, sha=body.get("sha"))
        return FileContent(path=path, text=text, size=size, sha=body.get("sha"))

    async def download_archive(self, full_name: str, sha: str, dest: Path) -> Path:
        """Download and safely extract the repository at exactly ``sha`` (no .git, no creds)."""
        if not is_valid_sha(sha):
            raise PolicyViolation("Archives can only be downloaded for a pinned commit SHA.")
        url = f"{self.api_url}{self._repo_path(full_name)}/tarball/{sha}"
        headers = await self._headers()
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
            archive = Path(handle.name)
        try:
            async with self._client.stream("GET", url, headers=headers, follow_redirects=True) as response:
                if response.is_error:
                    await response.aread()
                    http.raise_for_response("github", response)
                written = 0
                async with await anyio.open_file(archive, "wb") as out:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > self.max_tarball_bytes:
                            raise PolicyViolation("Repository archive exceeds GITHUB_MAX_TARBALL_BYTES.")
                        await out.write(chunk)
            skipped = await asyncio.to_thread(safe_extract_tarball, archive, dest)
            if skipped:
                logger.info("archive entries skipped", extra={"count": len(skipped)})
            return dest
        finally:
            await asyncio.to_thread(archive.unlink, missing_ok=True)

    async def changed_files_between(self, full_name: str, base: str, head: str) -> list[str]:
        response = await self._req(
            "GET", f"{self._repo_path(full_name)}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        )
        return [f["filename"] for f in response.json().get("files", []) if "filename" in f]

    # --- write operations ----------------------------------------------------------------

    async def create_branch_with_commit(
        self, full_name: str, *, base_sha: str, branch: str, default_branch: str,
        files: dict[str, str], file_modes: dict[str, str], message: str,
    ) -> str:
        validate_branch_name(branch)
        if not branch.startswith(BRANCH_PREFIX) or branch == default_branch:
            raise PolicyViolation("Patchora may only create new 'patchora/' branches.")
        if not is_valid_sha(base_sha):
            raise PolicyViolation("A pinned base commit SHA is required.")
        if not files:
            raise PolicyViolation("Refusing to publish an empty change.")
        repo = self._repo_path(full_name)
        base = await self.get_commit(full_name, base_sha)
        tree_items: list[dict[str, Any]] = []
        for path, content in sorted(files.items()):
            path = normalize_repo_path(path)
            mode = file_modes.get(path, "100644")
            if mode not in {"100644", "100755"}:
                raise PolicyViolation(f"Refusing to write {path} with mode {mode}.")
            blob = await self._req("POST", f"{repo}/git/blobs",
                                   json={"content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                                         "encoding": "base64"})
            tree_items.append({"path": path, "mode": mode, "type": "blob", "sha": blob.json()["sha"]})
        tree = await self._req("POST", f"{repo}/git/trees", json={"base_tree": base.tree_sha, "tree": tree_items})
        commit = await self._req("POST", f"{repo}/git/commits",
                                 json={"message": message, "tree": tree.json()["sha"], "parents": [base_sha]})
        commit_sha = str(commit.json()["sha"])
        # Creating a ref is not retried automatically: on an uncertain outcome the caller
        # queries the branch before trying again.
        ref = await self._req("POST", f"{repo}/git/refs",
                              json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
                              expected_errors=(422,), retry=False)
        if ref.status_code == 422:
            raise ConflictError(f"Branch {branch} already exists.")
        return commit_sha

    @staticmethod
    def _pr_info(body: dict[str, Any]) -> PullRequestInfo:
        return PullRequestInfo(
            number=body["number"], id=str(body["id"]), url=body["html_url"],
            state=body.get("state", "open"), merged=bool(body.get("merged") or body.get("merged_at")),
            draft=bool(body.get("draft")), head_branch=body["head"]["ref"], base_branch=body["base"]["ref"],
            head_sha=body["head"].get("sha"), title=body.get("title", ""),
        )

    async def find_pull_request(self, full_name: str, branch: str) -> PullRequestInfo | None:
        owner, _ = validate_full_name(full_name)
        validate_branch_name(branch)
        response = await self._req("GET", f"{self._repo_path(full_name)}/pulls",
                                   params={"head": f"{owner}:{branch}", "state": "all", "per_page": 5})
        items = response.json()
        return self._pr_info(items[0]) if items else None

    async def create_pull_request(self, full_name: str, *, title: str, body: str, head: str, base: str,
                                  draft: bool) -> PullRequestInfo:
        validate_branch_name(head)
        validate_branch_name(base)
        if not head.startswith(BRANCH_PREFIX):
            raise PolicyViolation("Pull requests may only be opened from 'patchora/' branches.")
        response = await self._req("POST", f"{self._repo_path(full_name)}/pulls",
                                   json={"title": title[:250], "body": body[:60_000], "head": head,
                                         "base": base, "draft": draft},
                                   expected_errors=(422,), retry=False)
        if response.status_code == 422:
            existing = await self.find_pull_request(full_name, head)
            if existing is not None:
                return existing
            raise ExternalServiceError(f"GitHub rejected the pull request: {response.text[:300]}",
                                       service="github", upstream_status=422)
        return self._pr_info(response.json())

    async def get_pull_request(self, full_name: str, number: int) -> PullRequestInfo:
        response = await self._req("GET", f"{self._repo_path(full_name)}/pulls/{int(number)}")
        return self._pr_info(response.json())

    async def find_issue_by_marker(self, full_name: str, marker: str) -> IssueInfo | None:
        owner, name = validate_full_name(full_name)
        if not marker.replace("-", "").isalnum():
            raise PolicyViolation("Invalid issue marker.")
        response = await self._req("GET", "/search/issues",
                                   params={"q": f'repo:{owner}/{name} is:issue in:body "{marker}"',
                                           "per_page": 5})
        for item in response.json().get("items", []):
            if marker in (item.get("body") or ""):
                return IssueInfo(number=item["number"], id=str(item["id"]), url=item["html_url"],
                                 state=item.get("state", "open"), title=item.get("title", ""))
        return None

    async def create_issue(self, full_name: str, *, title: str, body: str, labels: list[str]) -> IssueInfo:
        response = await self._req("POST", f"{self._repo_path(full_name)}/issues",
                                   json={"title": title[:250], "body": body[:60_000], "labels": labels[:5]},
                                   retry=False)
        item = response.json()
        return IssueInfo(number=item["number"], id=str(item["id"]), url=item["html_url"],
                         state=item.get("state", "open"), title=item.get("title", ""))
