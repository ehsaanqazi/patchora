"""A small in-memory GitHub + Sentry REST API for respx-based integration tests."""

from __future__ import annotations

import base64
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import respx

API = "https://api.github.com"
SENTRY = "https://sentry.io"


def tarball_bytes(root: Path, top: str = "acme-shop-api-0123abc") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts or ".pytest_cache" in path.parts:
                continue
            arcname = f"{top}/{path.relative_to(root).as_posix()}"
            tar.add(path, arcname=arcname, recursive=False)
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self, root: Path, full_name: str, sha: str, default_branch: str = "main") -> None:
        self.root = root
        self.full_name = full_name
        self.sha = sha
        self.default_branch = default_branch
        self.refs: dict[str, str] = {f"heads/{default_branch}": sha}
        self.commits: dict[str, dict[str, Any]] = {sha: {"tree": "tree-" + sha[:8], "message": "release"}}
        self.blobs: dict[str, str] = {}
        self.pulls: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_tree = False
        self.ref_updates: list[str] = []

    def _files(self) -> dict[str, bytes]:
        out = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts:
                out[path.relative_to(self.root).as_posix()] = path.read_bytes()
        return out

    def install(self, router: respx.MockRouter) -> None:
        repo = f"{API}/repos/{self.full_name}"

        def record(request: httpx.Request) -> None:
            self.calls.append((request.method, request.url.path))

        def commit(request: httpx.Request, ref: str) -> httpx.Response:
            record(request)
            sha = self.refs.get(f"heads/{ref}", ref)
            info = self.commits.get(sha)
            if info is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"sha": sha, "commit": {"tree": {"sha": info["tree"]},
                                                                     "message": info["message"]}})

        def get_ref(request: httpx.Request, ref: str) -> httpx.Response:
            record(request)
            sha = self.refs.get(f"heads/{ref}")
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"ref": f"refs/heads/{ref}", "object": {"sha": sha}})

        def tree(request: httpx.Request, sha: str) -> httpx.Response:
            record(request)
            if self.fail_tree:
                return httpx.Response(403, json={"message": "Resource not accessible by integration"})
            items = [{"path": p, "type": "blob", "mode": "100644", "size": len(b), "sha": "b" * 40}
                     for p, b in self._files().items()]
            return httpx.Response(200, json={"sha": sha, "tree": items, "truncated": False})

        def contents(request: httpx.Request, path: str) -> httpx.Response:
            record(request)
            data = self._files().get(path)
            if data is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": len(data),
                                             "sha": "c" * 40, "content": base64.b64encode(data).decode()})

        def tarball(request: httpx.Request, sha: str) -> httpx.Response:
            record(request)
            return httpx.Response(200, content=tarball_bytes(self.root))

        def blobs(request: httpx.Request) -> httpx.Response:
            record(request)
            sha = f"{len(self.blobs) + 1:040x}"
            self.blobs[sha] = base64.b64decode(json.loads(request.content)["content"]).decode()
            return httpx.Response(201, json={"sha": sha})

        def trees(request: httpx.Request) -> httpx.Response:
            record(request)
            return httpx.Response(201, json={"sha": "e" * 40})

        def commits(request: httpx.Request) -> httpx.Response:
            record(request)
            body = json.loads(request.content)
            sha = f"{len(self.commits) + 100:040x}"
            self.commits[sha] = {"tree": body["tree"], "message": body["message"], "parents": body["parents"]}
            return httpx.Response(201, json={"sha": sha})

        def create_ref(request: httpx.Request) -> httpx.Response:
            record(request)
            body = json.loads(request.content)
            name = body["ref"].removeprefix("refs/")
            if name in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[name] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})

        def update_ref(request: httpx.Request, ref: str) -> httpx.Response:  # must never be called
            self.ref_updates.append(ref)
            return httpx.Response(500)

        def list_pulls(request: httpx.Request) -> httpx.Response:
            record(request)
            head = request.url.params.get("head", "").split(":", 1)[-1]
            return httpx.Response(200, json=[p for p in self.pulls if p["head"]["ref"] == head])

        def create_pull(request: httpx.Request) -> httpx.Response:
            record(request)
            body = json.loads(request.content)
            number = len(self.pulls) + 1
            pr = {"number": number, "id": 9000 + number, "html_url": f"https://github.com/{self.full_name}/pull/{number}",
                  "state": "open", "draft": body.get("draft", False), "title": body["title"], "body": body["body"],
                  "head": {"ref": body["head"], "sha": self.refs.get(f"heads/{body['head']}")},
                  "base": {"ref": body["base"]}}
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)

        def search_issues(request: httpx.Request) -> httpx.Response:
            record(request)
            return httpx.Response(200, json={"items": self.issues})

        def create_issue(request: httpx.Request) -> httpx.Response:
            record(request)
            body = json.loads(request.content)
            number = len(self.issues) + 1
            item = {"number": number, "id": 7000 + number, "html_url": f"https://github.com/{self.full_name}/issues/{number}",
                    "state": "open", "title": body["title"], "body": body["body"]}
            self.issues.append(item)
            return httpx.Response(201, json=item)

        router.get(url__regex=rf"{repo}/commits/(?P<ref>[^/]+)$").mock(side_effect=commit)
        router.get(url__regex=rf"{repo}/git/ref/heads/(?P<ref>.+)$").mock(side_effect=get_ref)
        router.get(url__regex=rf"{repo}/git/trees/(?P<sha>[^/?]+)").mock(side_effect=tree)
        router.get(url__regex=rf"{repo}/contents/(?P<path>[^?]+)").mock(side_effect=contents)
        router.get(url__regex=rf"{repo}/tarball/(?P<sha>[^/]+)$").mock(side_effect=tarball)
        router.post(f"{repo}/git/blobs").mock(side_effect=blobs)
        router.post(f"{repo}/git/trees").mock(side_effect=trees)
        router.post(f"{repo}/git/commits").mock(side_effect=commits)
        router.post(f"{repo}/git/refs").mock(side_effect=create_ref)
        router.patch(url__regex=rf"{repo}/git/refs/(?P<ref>.+)$").mock(side_effect=update_ref)
        router.get(f"{repo}/pulls").mock(side_effect=list_pulls)
        router.post(f"{repo}/pulls").mock(side_effect=create_pull)
        router.get(f"{API}/search/issues").mock(side_effect=search_issues)
        router.post(f"{repo}/issues").mock(side_effect=create_issue)
        router.get(f"{repo}").mock(return_value=httpx.Response(200, json={
            "id": 4242, "name": self.full_name.split("/")[1], "full_name": self.full_name,
            "owner": {"login": self.full_name.split("/")[0]}, "default_branch": self.default_branch,
            "private": True, "permissions": {"push": True}}))


def install_sentry(router: respx.MockRouter, org: str, issue: dict[str, Any], event: dict[str, Any],
                   release_commits: list[dict[str, Any]]) -> None:
    base = f"{SENTRY}/api/0/organizations/{org}"
    router.get(f"{base}/issues/{issue['id']}/").mock(return_value=httpx.Response(200, json=issue))
    router.get(f"{base}/issues/{issue['id']}/events/latest/").mock(return_value=httpx.Response(200, json=event))
    router.get(url__regex=rf"{base}/releases/.+/commits/$").mock(
        return_value=httpx.Response(200, json=release_commits))
    router.get(f"{base}/").mock(return_value=httpx.Response(200, json={"slug": org, "name": org.title()}))
