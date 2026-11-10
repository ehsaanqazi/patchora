"""Demo-mode providers. They never contact external services.

* :class:`DemoIssueSource` serves the synthetic Sentry scenarios (through the real parsers).
* :class:`DemoCodeHost` reads the bundled fixture repository and *simulates* publication:
  branches, PRs and issues are recorded locally with ``demo://`` URLs and are never sent
  to GitHub.
* :func:`demo_sandbox` returns canned, clearly simulated test results; no code is executed.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from app.core.exceptions import ConflictError, ExternalNotFound, PolicyViolation
from app.demo.scenarios import (
    DEMO_ORG,
    DEMO_PROJECT,
    DEMO_REPO,
    DEMO_SHA,
    SCENARIOS,
    DemoScenario,
)
from app.providers.llm import ScriptedLLM
from app.sandbox.result import ExecutionResult
from app.sandbox.simulated_runner import SimulatedSandbox
from app.schemas.issues import NormalizedEvent, NormalizedIssue, ReleaseCommit
from app.schemas.repositories import (
    CommitInfo,
    FileContent,
    IssueInfo,
    PullRequestInfo,
    RepoInfo,
    TreeEntry,
)
from app.security.input_sanitization import normalize_repo_path, validate_branch_name
from app.services.github_service import BRANCH_PREFIX
from app.services.sentry_service import parse_event, parse_issue

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixture_repo"


def scenario_for_issue(sentry_issue_id: str) -> DemoScenario | None:
    return next((s for s in SCENARIOS.values() if s.issue["id"] == sentry_issue_id), None)


class DemoIssueSource:
    organization = DEMO_ORG
    simulated = True

    async def list_issues(self, project: str, *, query: str | None = None, cursor: str | None = None,
                          limit: int = 25) -> tuple[list[NormalizedIssue], str | None]:
        issues = [parse_issue(s.issue, DEMO_ORG) for s in SCENARIOS.values()]
        if query:
            issues = [i for i in issues if query.lower() in i.title.lower()]
        return issues[:limit], None

    async def get_issue(self, issue_id: str) -> NormalizedIssue:
        scenario = scenario_for_issue(issue_id)
        if scenario is None:
            raise ExternalNotFound(f"Demo issue {issue_id} not found", service="sentry-demo")
        return parse_issue(scenario.issue, DEMO_ORG)

    async def get_latest_event(self, issue_id: str) -> NormalizedEvent:
        scenario = scenario_for_issue(issue_id)
        if scenario is None:
            raise ExternalNotFound(f"Demo issue {issue_id} not found", service="sentry-demo")
        return parse_event(scenario.event)

    async def get_release_commits(self, version: str) -> list[ReleaseCommit]:
        return [ReleaseCommit(sha=DEMO_SHA, repository=DEMO_REPO, message="Release 1.4.2")]


class DemoCodeHost:
    """Local fixture repository. Publication is simulated and stays inside Patchora."""

    simulated = True

    def __init__(self, root: Path = FIXTURE_ROOT, full_name: str = DEMO_REPO, sha: str = DEMO_SHA) -> None:
        self.root = root
        self.full_name = full_name
        self.sha = sha
        self.branches: dict[str, str] = {}
        self.pulls: dict[str, PullRequestInfo] = {}
        self.issues: dict[str, IssueInfo] = {}

    def _check(self, full_name: str) -> None:
        if full_name != self.full_name:
            raise ExternalNotFound(f"{full_name} is not available in demo mode", service="github-demo")

    async def get_repository(self, full_name: str) -> RepoInfo:
        self._check(full_name)
        owner, name = full_name.split("/")
        return RepoInfo(provider_repository_id="demo-shop-api", owner=owner, name=name, full_name=full_name,
                        default_branch="main", private=True)

    async def list_repositories(self) -> list[RepoInfo]:
        return [await self.get_repository(self.full_name)]

    async def get_commit(self, full_name: str, ref: str) -> CommitInfo:
        self._check(full_name)
        if ref in (self.sha, "main"):
            return CommitInfo(sha=self.sha, tree_sha=self.sha, message="Release 1.4.2")
        if ref in self.branches.values() or ref in self.branches:
            return CommitInfo(sha=self.branches.get(ref, ref), tree_sha=self.sha, message="demo commit")
        raise ExternalNotFound(f"Unknown ref {ref}", service="github-demo")

    async def get_branch_head(self, full_name: str, branch: str) -> str | None:
        self._check(full_name)
        if branch == "main":
            return self.sha
        return self.branches.get(branch)

    async def get_tree(self, full_name: str, sha: str) -> list[TreeEntry]:
        self._check(full_name)
        entries: list[TreeEntry] = []
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root).as_posix()
            if "__pycache__" in rel or ".pytest_cache" in rel:
                continue
            if path.is_dir():
                entries.append(TreeEntry(path=rel, type="tree", mode="040000"))
            else:
                entries.append(TreeEntry(path=rel, type="blob", mode="100644", size=path.stat().st_size))
        return entries

    async def get_file(self, full_name: str, path: str, ref: str) -> FileContent:
        self._check(full_name)
        path = normalize_repo_path(path)
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root.resolve()) or not target.is_file():
            raise ExternalNotFound(f"{path} not found", service="github-demo")
        return FileContent(path=path, text=target.read_text(encoding="utf-8"), size=target.stat().st_size)

    async def download_archive(self, full_name: str, sha: str, dest: Path) -> Path:
        self._check(full_name)
        shutil.copytree(self.root, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        return dest

    async def changed_files_between(self, full_name: str, base: str, head: str) -> list[str]:
        return []

    async def create_branch_with_commit(self, full_name: str, *, base_sha: str, branch: str,
                                        default_branch: str, files: dict[str, str],
                                        file_modes: dict[str, str], message: str) -> str:
        self._check(full_name)
        validate_branch_name(branch)
        if not branch.startswith(BRANCH_PREFIX) or branch == default_branch:
            raise PolicyViolation("Patchora may only create new 'patchora/' branches.")
        if branch in self.branches:
            raise ConflictError(f"Branch {branch} already exists.")
        sha = hashlib.sha1(f"{branch}{sorted(files.items())}".encode(), usedforsecurity=False).hexdigest()
        self.branches[branch] = sha
        return sha

    async def find_pull_request(self, full_name: str, branch: str) -> PullRequestInfo | None:
        return self.pulls.get(branch)

    async def create_pull_request(self, full_name: str, *, title: str, body: str, head: str, base: str,
                                  draft: bool) -> PullRequestInfo:
        self._check(full_name)
        number = 100 + int(hashlib.sha1(head.encode(), usedforsecurity=False).hexdigest()[:3], 16) % 900
        info = PullRequestInfo(number=number, id=f"demo-pr-{number}",
                               url=f"demo://{full_name}/pull/{number}", state="open", draft=draft,
                               head_branch=head, base_branch=base, head_sha=self.branches.get(head),
                               title=title)
        self.pulls[head] = info
        return info

    async def get_pull_request(self, full_name: str, number: int) -> PullRequestInfo:
        for info in self.pulls.values():
            if info.number == number:
                return info
        raise ExternalNotFound("Demo pull request not found", service="github-demo")

    async def find_issue_by_marker(self, full_name: str, marker: str) -> IssueInfo | None:
        return self.issues.get(marker)

    async def create_issue(self, full_name: str, *, title: str, body: str, labels: list[str]) -> IssueInfo:
        self._check(full_name)
        number = 10 + len(self.issues)
        info = IssueInfo(number=number, id=f"demo-issue-{number}", url=f"demo://{full_name}/issues/{number}",
                         state="open", title=title)
        match = re.search(r"patchora-ref-[0-9a-f]+", body)
        self.issues[match.group(0) if match else f"issue-{number}"] = info
        return info


def demo_llm(scenario_key: str | None) -> ScriptedLLM:
    scenario = SCENARIOS.get(scenario_key or "")
    responses = {k: list(v) for k, v in (scenario.responses if scenario else {}).items()}
    return ScriptedLLM(responses=responses, model_name="scripted-demo-model (simulated)")


def demo_sandbox(scenario_key: str | None) -> SimulatedSandbox:
    scenario = SCENARIOS.get(scenario_key or "")
    behaviour = scenario.sandbox if scenario else {}

    def responder(workspace: Path, argv: list[str]) -> ExecutionResult:
        if argv[:2] == ["pip", "install"] or argv[:2] == ["npm", "ci"]:
            phase = "setup"
        elif "py_compile" in argv or argv[:2] == ["node", "--check"]:
            phase = "static"
        else:
            phase = "baseline" if workspace.name == "baseline" else "patched"
            if phase == "patched" and "patched_full" in behaviour and not any(a.startswith("tests/") for a in argv):
                phase = "patched_full"  # whole suite rather than the targeted test file
        exit_code, output = behaviour.get(phase, (0, "[simulated] ok"))
        return ExecutionResult(exit_code=exit_code, stdout=output, stderr="", duration_ms=0)

    return SimulatedSandbox(responder)


DEMO_MAPPING = {"sentry_organization": DEMO_ORG, "sentry_project": DEMO_PROJECT}
