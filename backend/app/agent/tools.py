"""Deterministic tools used by the agent nodes.

The model never calls these directly and never chooses their arguments without validation:
nodes call them with values derived from configuration, the pinned repository tree and
validated model output.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.enums import ValidationOutcome
from app.core.exceptions import ExternalNotFound
from app.sandbox.interface import Sandbox
from app.sandbox.limits import SandboxLimits
from app.schemas.agent_outputs import PatchProposal
from app.schemas.issues import ReleaseCommit
from app.security.input_sanitization import is_valid_sha, truncate, validate_branch_name
from app.services.github_service import BRANCH_PREFIX, CodeHost
from app.services.patch_service import (
    PatchError,
    PatchPolicy,
    apply_edits,
    apply_patch_to_workspace,
    build_unified_diff,
    is_test_path,
    parse_unified_diff,
)
from app.services.validation_service import (
    PhaseResult,
    TestPlan,
    copy_workspace,
    detect_test_plan,
    run_phase,
)

# --- Revision -------------------------------------------------------------------------------

@dataclass
class RevisionChoice:
    sha: str
    kind: str  # release_commit | branch_head
    note: str


async def resolve_revision(host: CodeHost, full_name: str, base_branch: str, release: str | None,
                           release_commits: list[ReleaseCommit]) -> RevisionChoice:
    """Pin the analysis to the release commit when Sentry knows it; otherwise the branch head,
    stated explicitly so the report never implies it matches the deployed release."""
    for commit in release_commits:
        if commit.repository and commit.repository.lower() != full_name.lower():
            continue
        if not is_valid_sha(commit.sha):
            continue
        try:
            info = await host.get_commit(full_name, commit.sha)
        except ExternalNotFound:
            continue
        return RevisionChoice(info.sha, "release_commit",
                              f"Commit {info.sha[:12]} from Sentry release '{release}'.")
    head = await host.get_branch_head(full_name, base_branch)
    if head is None:
        raise ExternalNotFound(f"Branch {base_branch} was not found in {full_name}", service="github")
    reason = (f"Sentry release '{release}' has no associated commit for this repository"
              if release else "The Sentry event has no release information")
    return RevisionChoice(head, "branch_head",
                          f"{reason}; analyzing the current head of '{base_branch}' ({head[:12]}). The deployed "
                          "code may differ from this revision.")


# --- Patch construction ---------------------------------------------------------------------

def build_patch(originals: dict[str, str | None], proposal: PatchProposal) -> tuple[str, dict[str, str]]:
    updated = apply_edits(originals, proposal.edits)
    if not updated:
        raise PatchError("The proposed edits do not change any file.")
    diff = build_unified_diff(originals, updated)
    if not diff:
        raise PatchError("The proposed edits produced an empty diff.")
    return diff, updated


def patch_policy(*, max_files: int, max_lines: int, protected: list[str], secrets: list[str],
                 investigation_tree: set[str], symlinks: set[str], submodules: set[str]) -> PatchPolicy:
    return PatchPolicy(max_files=max_files, max_lines=max_lines, protected_paths=protected,
                       known_secrets=secrets, symlink_paths=frozenset(symlinks),
                       submodule_paths=frozenset(submodules), existing_paths=frozenset(investigation_tree))


def extract_test_changes(patch_text: str) -> str | None:
    """The test-file portion of a patch, used to reproduce the failure on the base revision."""
    sections: list[str] = []
    current: list[str] = []
    current_path: str | None = None
    for line in patch_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current and current_path and is_test_path(current_path):
                sections.append("".join(current))
            current = [line]
            parts = line.split()
            current_path = parts[3][2:] if len(parts) >= 4 else None
        else:
            current.append(line)
    if current and current_path and is_test_path(current_path):
        sections.append("".join(current))
    return "".join(sections) or None


# --- Workspaces -----------------------------------------------------------------------------

@dataclass
class Workspaces:
    root: Path
    base: Path
    baseline: Path
    patched: Path
    baseline_includes_new_tests: bool
    setup_results: list[PhaseResult]
    applied: dict[str, str]


def workspace_dir(workspace_root: Path, run_id: int) -> Path:
    return workspace_root / f"run-{int(run_id)}"


def cleanup_workspace(workspace_root: Path, run_id: int) -> None:
    target = workspace_dir(workspace_root, run_id)
    if target.exists() and target.resolve().is_relative_to(workspace_root.resolve()):
        shutil.rmtree(target, ignore_errors=True)


async def prepare_workspaces(
    host: CodeHost, full_name: str, sha: str, workspace_root: Path, run_id: int, patch_text: str,
    *, sandbox: Sandbox, plan_factory: Any, limits: SandboxLimits, run_setup: bool,
) -> tuple[Workspaces, TestPlan]:
    """Download the pinned revision, install dependencies (setup phase), then create:
    ``baseline`` = base + test-only changes, ``patched`` = base + full patch."""
    root = workspace_dir(workspace_root, run_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    base = root / "base"
    base.mkdir(parents=True)
    await host.download_archive(full_name, sha, base)
    files_changed = [fp.path for fp in parse_unified_diff(patch_text)]
    plan: TestPlan = plan_factory(base, files_changed)
    setup_results: list[PhaseResult] = []
    if run_setup:
        setup_limits = SandboxLimits(timeout_seconds=limits.timeout_seconds * 2, cpus=limits.cpus,
                                     memory_mb=limits.memory_mb, pids_limit=limits.pids_limit,
                                     max_output_bytes=limits.max_output_bytes, network=True)
        for command in plan.setup:
            result = await run_phase(sandbox, base, "setup", command, setup_limits, plan.image)
            setup_results.append(result)
            if result.outcome != ValidationOutcome.PASSED:
                break
    baseline = root / "baseline"
    patched = root / "patched"
    copy_workspace(base, baseline)
    copy_workspace(base, patched)
    includes_tests = False
    test_patch = extract_test_changes(patch_text)
    if test_patch:
        try:
            apply_patch_to_workspace(baseline, test_patch)
            includes_tests = True
        except PatchError:
            shutil.rmtree(baseline, ignore_errors=True)
            copy_workspace(base, baseline)
    applied = apply_patch_to_workspace(patched, patch_text)
    return Workspaces(root, base, baseline, patched, includes_tests, setup_results, applied), plan


def make_plan_factory(*, repo_commands: list[dict[str, Any]] | None, test_targets: list[str],
                      python_image: str, node_image: str, allow_install: bool) -> Any:
    def factory(root: Path, changed: list[str]) -> TestPlan:
        return detect_test_plan(root, repo_commands=repo_commands, test_targets=test_targets,
                                changed_files=changed, python_image=python_image, node_image=node_image,
                                allow_dependency_install=allow_install)
    return factory


async def run_validation(sandbox: Sandbox, ws: Workspaces, plan: TestPlan,
                         limits: SandboxLimits) -> list[PhaseResult]:
    results: list[PhaseResult] = []
    commands = plan.targeted + plan.full
    # Baseline: only meaningful when it contains the new regression tests or for regression checks.
    for command in commands[:1]:
        results.append(await run_phase(sandbox, ws.baseline, "baseline", command, limits, plan.image))
    for index, command in enumerate(commands):
        result = await run_phase(sandbox, ws.patched, "patched", command, limits, plan.image)
        results.append(result)
        if result.outcome != ValidationOutcome.PASSED or index >= 1:
            break  # targeted first, then at most one broader run
    for command in plan.static:
        results.append(await run_phase(sandbox, ws.patched, "static", command, limits, plan.image))
    return results


# --- Publication helpers --------------------------------------------------------------------

def branch_name_for(sentry_issue_id: str, run_id: int, suffix: int = 0) -> str:
    safe_issue = "".join(c if c.isalnum() or c in "-_" else "-" for c in sentry_issue_id)[:40]
    name = f"{BRANCH_PREFIX}issue-{safe_issue}/run-{int(run_id)}" + (f"-{suffix}" if suffix else "")
    return validate_branch_name(name)


def commit_trailer(run_id: int, patch_id: int) -> str:
    return f"Patchora-Run: {run_id}\nPatchora-Patch: {patch_id}"


def issue_marker(organization: str, sentry_issue_id: str) -> str:
    digest = hashlib.sha256(f"{organization}/{sentry_issue_id}".encode()).hexdigest()[:16]
    return f"patchora-ref-{digest}"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {truncate(i, 400)}" for i in items) if items else "- None"


def render_pr_body(*, issue: dict[str, Any], permalink: str | None, diagnosis: dict[str, Any] | None,
                   plan: dict[str, Any] | None, evidence: list[dict[str, Any]], files: list[str],
                   diff_summary: str, validation: dict[str, Any], test_runs: list[dict[str, Any]],
                   policy_findings: list[dict[str, Any]], revision_note: str, diagnosis_status: str,
                   github_issue_url: str | None, simulated: list[str], run_id: int) -> str:
    top = (diagnosis or {}).get("hypotheses", [{}])[0] if diagnosis else {}
    cited = set(top.get("evidence_ids", []))
    evidence_lines = [f"`{e['path']}` lines {e['start_line']}-{e['end_line']} - {e['reason']}"
                      for e in evidence if e["id"] in cited][:6]
    tests = [f"`{' '.join(t['command'])[:120]}` ({t['phase']}): **{t['status']}**"
             + (f", {t['passed']} passed / {t['failed']} failed" if t.get("passed") is not None else "")
             for t in test_runs] or ["No tests were executed."]
    warnings = [f"{f['code']}: {f['message']}" for f in policy_findings if f.get("severity") == "warn"]
    status_text = {"supported_by_tests": "Supported by tests (a test failed before the fix and passes after it)",
                   "provisional": "Provisional (not confirmed by a reproducing test)"}.get(diagnosis_status,
                                                                                         diagnosis_status)
    sections = [
        "## Sentry issue",
        f"**{truncate(issue.get('title'), 200)}**" + (f" - [view in Sentry]({permalink})" if permalink else ""),
        f"Error type: `{issue.get('exception_type') or 'unknown'}` - events: {issue.get('event_count')}",
        "",
        "## Observed failure",
        truncate(((diagnosis or {}).get("issue_summary") or {}).get("summary") or "n/a", 1000),
        "",
        "## Root-cause hypothesis",
        f"{truncate(top.get('statement') or 'n/a', 1500)} (model confidence {top.get('confidence', 0):.2f})",
        f"Diagnosis status: **{status_text}**",
        "",
        "## Supporting code evidence",
        _bullets(evidence_lines),
        "",
        "## Proposed change",
        truncate((plan or {}).get("root_cause") or "", 800),
        f"Diff: {diff_summary}",
        "",
        "## Files modified",
        _bullets([f"`{f}`" for f in files]),
        "",
        "## Tests executed",
        _bullets(tests),
        f"Validation outcome: **{validation.get('outcome')}** - {validation.get('summary') or ''}",
        "",
        "## Security checks",
        "Path, protected-file, size, binary/mode-change, symlink and secret checks passed."
        + ("\nWarnings:\n" + _bullets(warnings) if warnings else ""),
        "",
        "## Known limitations",
        _bullets([revision_note, *((diagnosis or {}).get("missing_evidence") or []),
                  *(plan or {}).get("regression_risks", [])]),
    ]
    if github_issue_url:
        sections += ["", f"Related issue: {github_issue_url}"]
    if simulated:
        sections += ["", f"> Simulated components in this run: {', '.join(simulated)}"]
    sections += [
        "",
        "---",
        "_This change was generated with AI assistance by Patchora and requires human review. "
        f"It has not been merged or deployed. Run #{run_id}._",
    ]
    return "\n".join(sections)


def render_issue_body(*, issue: dict[str, Any], permalink: str | None, event: dict[str, Any] | None,
                      diagnosis: dict[str, Any] | None, revision_note: str | None, status: str,
                      marker: str) -> str:
    frames: list[str] = []
    if event:
        for exc in (event.get("exceptions") or [])[-1:]:
            frames.append(f"{exc.get('type')}: {truncate(exc.get('value'), 300)}")
            for frame in (exc.get("frames") or [])[-8:]:
                frames.append(f"  {frame.get('filename')}:{frame.get('lineno')} in {frame.get('function')}")
    top = (diagnosis or {}).get("hypotheses", [{}])[0] if diagnosis else {}
    return "\n".join([
        "## Summary",
        truncate(issue.get("title"), 300),
        "",
        f"Sentry: {permalink or 'n/a'}",
        f"Error type: `{issue.get('exception_type') or 'unknown'}`",
        f"Affected release: {(event or {}).get('release') or 'unknown'}",
        f"Revision investigated: {revision_note or 'n/a'}",
        "",
        "## Stack trace excerpt",
        "```",
        truncate("\n".join(frames) or "No stack trace available.", 3000),
        "```",
        "",
        "## Reproduction",
        "No verified reproduction steps are available.",
        "",
        "## Preliminary diagnosis",
        truncate(top.get("statement") or "Not yet established.", 1500),
        "",
        "## Evidence and uncertainty",
        truncate((diagnosis or {}).get("uncertainty") or "Diagnosis is provisional.", 1500),
        "",
        "## Investigation status",
        status,
        "",
        f"<!-- {marker} -->",
        "_Created by Patchora (AI-assisted)._",
    ])
