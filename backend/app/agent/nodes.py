"""Agent nodes: one handler per workflow state.

A handler performs the work of its state and returns the next :class:`Transition`. Handlers
never hold a database session across an external call; they use ``ctx.db`` for short
transactions. The engine (:mod:`app.agent.graph`) persists the transition, step record and
events atomically after each handler returns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import policies, prompts, tools
from app.agent.state import AgentState, RepositoryRef
from app.config import AutonomyMode
from app.core.enums import (
    ApprovalStatus,
    ApprovalType,
    PolicyStatus,
    RunOutcome,
    RunState,
    ValidationOutcome,
)
from app.core.exceptions import ConflictError, ExternalNotFound, PolicyViolation
from app.db.models import (
    AnalysisRun,
    Approval,
    GitHubIssue,
    ProposedPatch,
    PullRequest,
    Repository,
    RepositoryMapping,
    SentryIssue,
    TestExecution,
)
from app.db.session import with_write_retry
from app.providers.factory import RunDependencies
from app.sandbox.limits import SandboxLimits
from app.schemas.agent_outputs import Diagnosis, PatchPlan, PatchProposal, ValidationSummary
from app.schemas.issues import NormalizedEvent, NormalizedIssue, ReleaseCommit
from app.schemas.repositories import TreeEntry
from app.security.authorization import ensure_repository_allowed
from app.security.input_sanitization import normalize_repo_path, truncate
from app.services import audit_service, run_service
from app.services.groq_service import estimate_cost
from app.services.patch_service import (
    PatchError,
    apply_patch_to_texts,
    assess_risk,
    parse_unified_diff,
    summarize_diff,
    validate_patch,
)
from app.services.repository_service import EvidenceItem, Investigation, Investigator, is_sensitive
from app.services.validation_service import PhaseResult, TestPlan, evaluate

M = TypeVar("M", bound=BaseModel)
T = TypeVar("T")

RETRYABLE_PATCH_FINDINGS = {"malformed_patch", "unknown_file", "file_exists"}


@dataclass
class Transition:
    target: RunState
    summary: str = ""
    outcome: RunOutcome | None = None
    event_type: str | None = None
    message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunSnapshot:
    id: int
    status: str
    autonomy_mode: AutonomyMode
    is_demo: bool
    trigger: str
    issue_pk: int
    sentry_issue_id: str
    organization: str
    project: str
    repository_id: int | None
    repair_attempt: int
    source_revision: str | None


@dataclass
class NodeContext:
    run: RunSnapshot
    state: AgentState
    deps: RunDependencies
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    run_updates: dict[str, Any] = field(default_factory=dict)
    step_meta: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=lambda: {"prompt": 0, "completion": 0})
    _tree: list[TreeEntry] | None = None

    def event(self, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event_type, message, data or {}))

    async def db(self, fn: Callable[[Session], T]) -> T:
        """A short transaction in a worker thread, so SQLite lock waits never block the event loop."""
        return await asyncio.to_thread(with_write_retry, fn)

    async def ask(self, *, system: str, user: str, schema: type[M], purpose: str) -> M:
        llm = self.deps.require_llm()
        result = await llm.complete_json(system=system, user=user, schema=schema, purpose=purpose)
        self.usage["prompt"] += result.prompt_tokens
        self.usage["completion"] += result.completion_tokens
        self.usage["model"] = result.model
        self.usage["simulated"] = result.simulated
        self.step_meta.setdefault("llm_calls", []).append({
            "purpose": purpose, "model": result.model, "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens, "duration_ms": result.duration_ms,
            "attempts": result.attempts, "simulated": result.simulated,
        })
        return result.data

    async def tree(self) -> list[TreeEntry]:
        if self._tree is None:
            assert self.state.repository and self.state.revision
            self._tree = await self.deps.require_code_host().get_tree(self.state.repository.full_name,
                                                                      self.state.revision)
        return self._tree

    async def tree_sets(self) -> tuple[set[str], set[str], set[str], dict[str, str]]:
        paths: set[str] = set()
        symlinks: set[str] = set()
        submodules: set[str] = set()
        modes: dict[str, str] = {}
        for entry in await self.tree():
            if entry.is_submodule:
                submodules.add(entry.path)
            elif entry.type == "blob":
                paths.add(entry.path)
                modes[entry.path] = entry.mode
                if entry.is_symlink:
                    symlinks.add(entry.path)
        return paths, symlinks, submodules, modes

    async def file_texts(self, paths: list[str]) -> dict[str, str | None]:
        """Current content of files at the pinned revision (None for files that do not exist)."""
        assert self.state.repository and self.state.revision
        tree_paths, symlinks, _, _ = await self.tree_sets()
        host = self.deps.require_code_host()
        out: dict[str, str | None] = {}
        for raw in paths:
            path = normalize_repo_path(raw)
            if path in out:
                continue
            if path not in tree_paths:
                out[path] = None
                continue
            if path in symlinks or is_sensitive(path):
                raise PolicyViolation(f"{path} cannot be modified (symlink or sensitive file).")
            content = await host.get_file(self.state.repository.full_name, path, self.state.revision)
            if content.text is None:
                raise PolicyViolation(f"{path} is binary, too large or not a regular file.")
            out[path] = content.text
        return out

    @property
    def limits(self) -> SandboxLimits:
        s = self.deps.settings
        return SandboxLimits(timeout_seconds=self.deps.runtime.max_test_runtime_seconds, cpus=s.sandbox_cpus,
                             memory_mb=s.sandbox_memory_mb, pids_limit=s.sandbox_pids_limit,
                             max_output_bytes=s.sandbox_max_output_bytes)


Handler = Callable[[NodeContext], Awaitable[Transition]]


# --- RECEIVED / VALIDATING / DEDUPLICATING -------------------------------------------------

async def received(ctx: NodeContext) -> Transition:
    ctx.run_updates["started_at"] = True
    return Transition(RunState.VALIDATING, "Run started", event_type="run_started", message="Run started")


async def validating(ctx: NodeContext) -> Transition:
    problems: list[str] = []
    if ctx.deps.issue_source is None:
        problems.append(ctx.deps.issue_source_error or "Sentry is not configured.")
    if ctx.deps.llm is None:
        problems.append(ctx.deps.llm_error or "No LLM provider is configured.")
    if problems:
        return Transition(RunState.FAILED, "Configuration incomplete", RunOutcome.ERROR,
                          error=" ".join(problems), event_type="run_failed", message=" ".join(problems))
    ctx.step_meta["autonomy_mode"] = ctx.run.autonomy_mode.value
    return Transition(RunState.DEDUPLICATING, f"Request valid (mode {ctx.run.autonomy_mode.value})")


async def deduplicating(ctx: NodeContext) -> Transition:
    def check(session: Session) -> str | None:
        other = session.scalar(select(AnalysisRun).where(
            AnalysisRun.sentry_issue_id == ctx.run.issue_pk,
            AnalysisRun.id != ctx.run.id,
            AnalysisRun.status.in_(run_service.ACTIVE_STATES),
        ))
        if other is not None and other.id < ctx.run.id:
            return f"Run #{other.id} is already analyzing this issue."
        open_pr = session.scalar(
            select(PullRequest).join(AnalysisRun, PullRequest.run_id == AnalysisRun.id)
            .where(AnalysisRun.sentry_issue_id == ctx.run.issue_pk, PullRequest.status == "open")
        )
        if open_pr is not None:
            return f"An open Patchora pull request already exists for this issue: {open_pr.url}"
        return None

    duplicate = await ctx.db(check)
    if duplicate:
        return Transition(RunState.COMPLETED, duplicate, RunOutcome.DUPLICATE, event_type="run_completed",
                          message=duplicate)
    return Transition(RunState.FETCHING_SENTRY_DATA, "No duplicate work found")


# --- FETCHING_SENTRY_DATA / MAPPING_REPOSITORY ---------------------------------------------

async def fetching_sentry_data(ctx: NodeContext) -> Transition:
    source = ctx.deps.require_issue_source()
    issue: NormalizedIssue = await source.get_issue(ctx.run.sentry_issue_id)
    event: NormalizedEvent | None = None
    try:
        event = await source.get_latest_event(ctx.run.sentry_issue_id)
    except ExternalNotFound:
        ctx.state.event_error = "No event is available for this issue."
    release_commits: list[ReleaseCommit] = []
    if event and event.release:
        try:
            release_commits = await source.get_release_commits(event.release)
        except ExternalNotFound:
            ctx.state.add_note(f"Sentry has no commit data for release '{event.release}'.")

    def store(session: Session) -> None:
        run_service.upsert_issue(session, issue, is_demo=ctx.run.is_demo, event=event)

    await ctx.db(store)
    ctx.state.issue = issue.model_dump(mode="json")
    ctx.state.event = event.model_dump(mode="json") if event else None
    ctx.state.release = event.release if event else None
    ctx.state.release_commits = [c.model_dump() for c in release_commits]  # type: ignore[attr-defined]
    frames = len(event.primary_exception.frames) if event and event.primary_exception else 0
    return Transition(RunState.MAPPING_REPOSITORY,
                      f"Retrieved issue and latest event ({frames} frames, release {ctx.state.release})",
                      event_type="sentry_issue_retrieved", message=f"Sentry issue retrieved: {issue.title[:120]}",
                      data={"frames": frames, "release": ctx.state.release})


async def mapping_repository(ctx: NodeContext) -> Transition:
    def resolve(session: Session) -> tuple[Repository | None, RepositoryMapping | None]:
        mapping = run_service.find_mapping(session, ctx.run.organization, ctx.run.project)
        repo_id = ctx.run.repository_id or (mapping.repository_id if mapping else None)
        if repo_id is None:
            issue = session.get(SentryIssue, ctx.run.issue_pk)
            repo_id = issue.repository_id if issue else None
        repo = session.get(Repository, repo_id) if repo_id else None
        if repo is not None:
            session.expunge(repo)
        if mapping is not None:
            session.expunge(mapping)
        return repo, mapping

    repo, mapping = await ctx.db(resolve)
    if repo is None:
        msg = (f"No repository is mapped for Sentry project {ctx.run.organization}/{ctx.run.project}. "
               "Configure a repository mapping and retry.")
        return Transition(RunState.NEEDS_HUMAN_REVIEW, msg, RunOutcome.NO_REPOSITORY,
                          event_type="needs_human_review", message=msg)
    if repo.is_demo != ctx.run.is_demo:
        raise PolicyViolation("Demo and real data cannot be mixed: the mapped repository does not match "
                              "the run type.")
    ensure_repository_allowed(repo)
    base_branch = (mapping.branch if mapping and mapping.branch else None) or repo.default_branch
    host = ctx.deps.require_code_host()
    commits = [ReleaseCommit.model_validate(c) for c in getattr(ctx.state, "release_commits", []) or []]
    choice = await tools.resolve_revision(host, repo.full_name, base_branch, ctx.state.release, commits)
    ctx.state.repository = RepositoryRef(id=repo.id, full_name=repo.full_name,
                                         default_branch=repo.default_branch, base_branch=base_branch,
                                         provider=repo.provider)
    ctx.state.revision = choice.sha
    ctx.state.revision_kind = choice.kind
    ctx.state.revision_note = choice.note
    ctx.state.mapping = {  # type: ignore[attr-defined]
        "strip_prefixes": list(mapping.strip_prefixes_json) if mapping else [],
        "path_prefixes": list(mapping.path_prefixes_json) if mapping else [],
    }
    ctx.run_updates.update(repository_id=repo.id, source_revision=choice.sha,
                           source_revision_kind=choice.kind, base_branch=base_branch)
    return Transition(RunState.FETCHING_SOURCE, f"{repo.full_name} @ {choice.sha[:12]} ({choice.kind})",
                      event_type="repository_resolved",
                      message=f"Repository resolved: {repo.full_name} @ {choice.sha[:12]}",
                      data={"repository": repo.full_name, "revision": choice.sha, "kind": choice.kind})


# --- FETCHING_SOURCE / INVESTIGATING / DIAGNOSIS_READY -------------------------------------

def _investigator(ctx: NodeContext) -> Investigator:
    assert ctx.state.repository and ctx.state.revision
    mapping = getattr(ctx.state, "mapping", None) or {}
    budget = int(ctx.deps.require_llm().input_budget_chars * 0.55)
    return Investigator(ctx.deps.require_code_host(), ctx.state.repository.full_name, ctx.state.revision,
                        strip_prefixes=mapping.get("strip_prefixes", []),
                        path_prefixes=mapping.get("path_prefixes", []), budget_chars=budget)


async def fetching_source(ctx: NodeContext) -> Transition:
    investigator = _investigator(ctx)
    event = NormalizedEvent.model_validate(ctx.state.event) if ctx.state.event else NormalizedEvent(event_id="")
    inv = await investigator.run(event)
    if ctx.deps.semantic_index is not None and ctx.state.repository and ctx.state.revision:
        exception = event.primary_exception
        query = f"{exception.type}: {exception.value}" if exception else (ctx.state.issue or {}).get("title", "")
        try:
            hits = ctx.deps.semantic_index.search(ctx.state.repository.id, ctx.state.revision, query or "", k=4,
                                                  keyword_boost=[f.function or "" for f in inv.frames])
            investigator.add_semantic_hits(inv, hits)
        except Exception as exc:  # optional component: never fail the run because of it
            inv.notes.append(f"Semantic search unavailable: {type(exc).__name__}")
    ctx.state.investigation = inv.to_state()
    mapped = sum(1 for f in inv.frames if f.repo_path)
    ctx.step_meta["files_inspected"] = sorted(inv.files)[:30]
    return Transition(RunState.INVESTIGATING,
                      f"{len(inv.evidence)} evidence items from {len(inv.files)} files; "
                      f"{mapped}/{len(inv.frames)} app frames mapped",
                      event_type="source_inspected",
                      message=f"Inspected {len(inv.files)} source files ({len(inv.evidence)} evidence items)",
                      data={"files": sorted(inv.files)[:20], "notes": inv.notes[:5]})


def _rehydrate(ctx: NodeContext, tree_paths: set[str], symlinks: set[str]) -> tuple[Investigation, Investigator]:
    data = ctx.state.investigation or {}
    inv = Investigation(revision=ctx.state.revision or "")
    inv.evidence = [EvidenceItem(**e) for e in data.get("evidence", [])]
    inv.notes = list(data.get("notes", []))
    inv.related_tests = list(data.get("related_tests", []))
    inv.known_test_files = list(data.get("known_test_files", []))
    inv.tree_paths = tree_paths
    inv.symlinks = symlinks
    investigator = _investigator(ctx)
    investigator._counter = max((int(e.id[1:]) for e in inv.evidence if e.id[1:].isdigit()), default=0)
    investigator._used_chars = sum(len(e.excerpt) for e in inv.evidence)
    return inv, investigator


async def investigating(ctx: NodeContext) -> Transition:
    assert ctx.state.issue is not None
    revision_note = ctx.state.revision_note or "unknown"
    system, user = prompts.diagnosis_prompt(ctx.state.issue, ctx.state.event, ctx.state.investigation or {},
                                            revision_note, 1)
    diagnosis = await ctx.ask(system=system, user=user, schema=Diagnosis, purpose="diagnosis")
    if diagnosis.additional_files_requested:
        tree_paths, symlinks, _, _ = await ctx.tree_sets()
        inv, investigator = _rehydrate(ctx, tree_paths, symlinks)
        added = await investigator.add_requested_files(inv, diagnosis.additional_files_requested)
        if added:
            ctx.state.investigation = {**(ctx.state.investigation or {}),
                                       "evidence": [e.as_dict() for e in inv.evidence], "notes": inv.notes}
            ctx.event("evidence_expanded", f"Retrieved {len(added)} additional file(s) requested by the diagnosis",
                      {"files": added})
            system, user = prompts.diagnosis_prompt(ctx.state.issue, ctx.state.event, ctx.state.investigation,
                                                    revision_note, 2)
            diagnosis = await ctx.ask(system=system, user=user, schema=Diagnosis, purpose="diagnosis")
    evidence_ids = {e["id"] for e in (ctx.state.investigation or {}).get("evidence", [])}
    diagnosis, warnings = policies.sanitize_diagnosis(diagnosis, evidence_ids)
    for warning in warnings:
        ctx.state.add_note(warning)
    ctx.state.diagnosis = diagnosis.model_dump(mode="json")
    top = diagnosis.hypotheses[0]
    ctx.run_updates.update(root_cause_summary=truncate(top.statement, 2000), confidence_score=top.confidence,
                           diagnosis_status="provisional")
    return Transition(RunState.DIAGNOSIS_READY,
                      f"{diagnosis.error_classification}: {truncate(top.statement, 300)}",
                      event_type="diagnosis_generated",
                      message=f"Diagnosis generated ({diagnosis.error_classification}, "
                              f"confidence {top.confidence:.2f})",
                      data={"classification": diagnosis.error_classification, "confidence": top.confidence})


async def diagnosis_ready(ctx: NodeContext) -> Transition:
    diagnosis = Diagnosis.model_validate(ctx.state.diagnosis)
    gate = policies.evidence_gate(diagnosis, ctx.state.investigation or {})
    if not gate.ok:
        external = diagnosis.error_classification in {"external_dependency", "infrastructure", "configuration"}
        outcome = RunOutcome.ABSTAINED_EXTERNAL_CAUSE if external else RunOutcome.ABSTAINED_INSUFFICIENT_EVIDENCE
        ctx.state.abstain_reasons = gate.reasons  # type: ignore[attr-defined]
        message = "No patch attempted: " + " ".join(gate.reasons)
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, outcome, event_type="needs_human_review",
                          message=truncate(message, 900))
    return Transition(RunState.PLANNING_PATCH, "Evidence is sufficient to plan a fix")


# --- PLANNING / GENERATING / REVIEWING -----------------------------------------------------

async def planning_patch(ctx: NodeContext) -> Transition:
    tree_paths, _, _, _ = await ctx.tree_sets()
    inv = ctx.state.investigation or {}
    system, user = prompts.plan_prompt(ctx.state.issue or {}, inv, ctx.state.diagnosis or {},
                                       inv.get("known_test_files", []), ctx.deps.runtime.max_patch_files,
                                       ctx.deps.runtime.max_patch_lines)
    plan = await ctx.ask(system=system, user=user, schema=PatchPlan, purpose="patch_plan")
    ctx.state.plan = plan.model_dump(mode="json")
    gate = policies.plan_gate(plan, tree_paths, ctx.deps.runtime.protected_paths, ctx.deps.runtime.max_patch_files)
    if not gate.ok:
        protected = any("protected" in r for r in gate.reasons)
        outcome = (RunOutcome.POLICY_BLOCKED if protected else
                   RunOutcome.ABSTAINED_INSUFFICIENT_EVIDENCE if not plan.should_patch else RunOutcome.PATCH_INVALID)
        message = "Patch plan rejected: " + " ".join(gate.reasons)
        ctx.step_meta["policy_findings"] = gate.reasons
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, outcome,
                          event_type="patch_validation_failed" if protected else "needs_human_review",
                          message=truncate(message, 900))
    files = [f.path for f in plan.files_to_modify]
    return Transition(RunState.GENERATING_PATCH, f"Plan: modify {', '.join(files)} (risk {plan.risk_level})",
                      event_type="patch_planned", message=f"Patch plan ready ({len(files)} file(s))")


def _retry_or_stop(ctx: NodeContext, reason: str, outcome: RunOutcome) -> Transition:
    ctx.state.patch_feedback.append(truncate(reason, 1500))
    if ctx.run.repair_attempt < ctx.deps.runtime.max_agent_retries:
        attempt = ctx.run.repair_attempt + 1
        ctx.run_updates["repair_attempt"] = attempt
        return Transition(RunState.GENERATING_PATCH, f"Retrying patch generation: {truncate(reason, 300)}",
                          event_type="retry_started",
                          message=f"Repair attempt {attempt} of {ctx.deps.runtime.max_agent_retries}",
                          data={"reason": truncate(reason, 300)})
    message = f"Stopped after {ctx.run.repair_attempt + 1} attempt(s): {truncate(reason, 600)}"
    return Transition(RunState.NEEDS_HUMAN_REVIEW, message, outcome, event_type="needs_human_review",
                      message=message)


async def generating_patch(ctx: NodeContext) -> Transition:
    plan = PatchPlan.model_validate(ctx.state.plan)
    allowed = [f.path for f in plan.files_to_modify] + [t.path for t in plan.tests_to_add_or_update]
    originals = await ctx.file_texts(allowed)
    shown = {p: t for p, t in originals.items() if t is not None}
    system, user = prompts.patch_prompt(ctx.state.investigation or {}, ctx.state.plan or {}, shown,
                                        ctx.state.patch_feedback, int(ctx.deps.require_llm().input_budget_chars * 0.6))
    proposal = await ctx.ask(system=system, user=user, schema=PatchProposal, purpose="patch")
    try:
        outside = [e.path for e in proposal.edits if normalize_repo_path(e.path) not in originals]
        if outside:
            raise PatchError(f"Edits touch files outside the approved plan: {outside}")
        diff, _ = tools.build_patch(originals, proposal)
    except PatchError as exc:
        return _retry_or_stop(ctx, f"The proposed edits could not be applied: {exc}", RunOutcome.PATCH_INVALID)
    except Exception as exc:  # invalid paths from the model
        return _retry_or_stop(ctx, f"The proposed edits are invalid: {exc}", RunOutcome.PATCH_INVALID)

    attempt = ctx.run.repair_attempt

    def persist(session: Session) -> int:
        patch = session.scalar(select(ProposedPatch).where(ProposedPatch.run_id == ctx.run.id,
                                                           ProposedPatch.attempt_number == attempt))
        if patch is None:
            patch = ProposedPatch(run_id=ctx.run.id, attempt_number=attempt, patch_content=diff)
            session.add(patch)
        for older in session.scalars(select(ProposedPatch).where(ProposedPatch.run_id == ctx.run.id,
                                                                 ProposedPatch.attempt_number < attempt)):
            older.superseded = True
        patch.patch_content = diff
        patch.base_commit_sha = ctx.state.revision
        patch.plan_json = {**(ctx.state.plan or {}), "summary": proposal.summary,
                           "tests_added": proposal.tests_added}
        patch.validation_status = ValidationOutcome.PENDING.value
        patch.policy_status = PolicyStatus.PENDING.value
        session.flush()
        return patch.id

    ctx.state.current_patch_id = await ctx.db(persist)
    return Transition(RunState.REVIEWING_PATCH, truncate(proposal.summary, 400), event_type="patch_generated",
                      message=f"Patch generated (attempt {attempt + 1})", data={"patch_id": ctx.state.current_patch_id})


async def reviewing_patch(ctx: NodeContext) -> Transition:
    tree_paths, symlinks, submodules, _ = await ctx.tree_sets()
    patch_id = ctx.state.current_patch_id
    patch_text = await ctx.db(lambda s: s.get(ProposedPatch, patch_id).patch_content)  # type: ignore[union-attr]
    runtime = ctx.deps.runtime
    policy = tools.patch_policy(max_files=runtime.max_patch_files, max_lines=runtime.max_patch_lines,
                                protected=runtime.protected_paths, secrets=ctx.deps.known_secrets,
                                investigation_tree=tree_paths, symlinks=symlinks, submodules=submodules)
    report = validate_patch(patch_text, policy)
    risk = assess_risk(report, (ctx.state.plan or {}).get("risk_level"))
    findings = [f.as_dict() for f in report.findings]

    def persist(session: Session) -> None:
        patch = session.get(ProposedPatch, patch_id)
        assert patch is not None
        patch.files_changed_json = report.paths
        patch.lines_added = report.lines_added
        patch.lines_removed = report.lines_removed
        patch.diff_summary = summarize_diff(report) if report.files else None
        patch.policy_findings_json = findings
        patch.policy_status = PolicyStatus.FAILED.value if report.blocked else PolicyStatus.PASSED.value
        patch.risk_level = risk

    await ctx.db(persist)
    ctx.step_meta["policy_findings"] = findings
    if report.blocked:
        codes = {f.code for f in report.findings if f.severity == "block"}
        reason = "; ".join(f"{f.code}: {f.message} {f.path or ''}".strip() for f in report.findings
                           if f.severity == "block")
        if codes <= RETRYABLE_PATCH_FINDINGS:
            return _retry_or_stop(ctx, f"Patch failed validation: {reason}", RunOutcome.PATCH_INVALID)
        message = f"Patch blocked by security policy: {truncate(reason, 700)}"
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.POLICY_BLOCKED,
                          event_type="patch_validation_failed", message=message)

    if ctx.run.autonomy_mode == AutonomyMode.ANALYZE_ONLY:
        return Transition(RunState.COMPLETED, "Analyze-only mode: patch preview produced, nothing executed",
                          RunOutcome.ANALYSIS_ONLY, event_type="run_completed",
                          message="Analysis complete (Mode A: no execution or publication)")
    available, reason = await ctx.deps.sandbox.available()
    if not available:
        ctx.state.validation.outcome = ValidationOutcome.NOT_RUN.value
        ctx.state.validation.summary = reason

        def mark(session: Session) -> None:
            patch = session.get(ProposedPatch, patch_id)
            assert patch is not None
            patch.validation_status = ValidationOutcome.NOT_RUN.value
            session.add(TestExecution(run_id=ctx.run.id, patch_id=patch_id, phase="patched", backend="none",
                                      status=ValidationOutcome.NOT_RUN.value,
                                      command_summary="(execution disabled)", stdout_summary=reason))

        await ctx.db(mark)
        message = (f"Patch preview ready, but code execution is unavailable ({reason}). Tests were not run "
                   "and publication is disabled until a sandbox is configured.")
        return Transition(RunState.COMPLETED, message, RunOutcome.ANALYSIS_ONLY, event_type="run_completed",
                          message=truncate(message, 900))
    return Transition(RunState.APPLYING_PATCH, f"Policy checks passed (risk {risk}): {summarize_diff(report)}",
                      event_type="patch_reviewed", message=f"Patch passed policy checks (risk {risk})")


# --- APPLYING / RUNNING / EVALUATING -------------------------------------------------------

async def _plan_factory(ctx: NodeContext) -> Any:
    repo_commands = await ctx.db(lambda s: s.get(Repository, ctx.state.repository.id).test_commands_json  # type: ignore[union-attr]
                           if ctx.state.repository else None)
    targets = ((ctx.state.plan or {}).get("test_recommendations") or {}).get("test_files", [])
    targets = list(targets) + [t["path"] for t in (ctx.state.plan or {}).get("tests_to_add_or_update", [])]
    ctx.state.validation.test_targets = targets[:10]
    return tools.make_plan_factory(repo_commands=repo_commands, test_targets=targets,
                                   python_image=ctx.deps.settings.sandbox_python_image,
                                   node_image=ctx.deps.settings.sandbox_node_image,
                                   allow_install=ctx.deps.runtime.sandbox_allow_dependency_install)


def _record_phase(session: Session, run_id: int, patch_id: int | None, phase: PhaseResult) -> None:
    session.add(TestExecution(
        run_id=run_id, patch_id=patch_id, phase=phase.phase, backend=phase.result.backend,
        status=phase.outcome.value, command_summary=truncate(" ".join(phase.command), 1000),
        exit_code=phase.result.exit_code, tests_passed=phase.tests_passed, tests_failed=phase.tests_failed,
        stdout_summary=truncate(phase.result.stdout, 16_000), stderr_summary=truncate(phase.result.stderr, 8_000),
        duration_ms=phase.result.duration_ms, timed_out=phase.result.timed_out,
        truncated_output=phase.result.truncated, simulated=phase.result.simulated,
    ))


def _record_phases(session: Session, run_id: int, patch_id: int | None, phases: list[PhaseResult]) -> None:
    for phase in phases:
        _record_phase(session, run_id, patch_id, phase)


async def _prepare(ctx: NodeContext) -> tuple[tools.Workspaces, TestPlan]:
    assert ctx.state.repository and ctx.state.revision
    patch_id = ctx.state.current_patch_id
    patch_text = await ctx.db(lambda s: s.get(ProposedPatch, patch_id).patch_content)  # type: ignore[union-attr]
    return await tools.prepare_workspaces(
        ctx.deps.require_code_host(), ctx.state.repository.full_name, ctx.state.revision,
        ctx.deps.workspace_root, ctx.run.id, patch_text, sandbox=ctx.deps.sandbox,
        plan_factory=await _plan_factory(ctx), limits=ctx.limits,
        # Dependency installation needs network access, so it only runs in an isolated container
        # (or is simulated in demo mode). The local development runner never installs packages.
        run_setup=ctx.deps.sandbox.isolation in {"container", "simulated"},
    )


async def applying_patch(ctx: NodeContext) -> Transition:
    try:
        workspaces, plan = await _prepare(ctx)
    except PatchError as exc:
        message = f"The patch could not be applied to the downloaded revision: {exc}"
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.PATCH_INVALID,
                          event_type="patch_validation_failed", message=truncate(message, 900))
    patch_id = ctx.state.current_patch_id
    await ctx.db(lambda s: _record_phases(s, ctx.run.id, patch_id, workspaces.setup_results))
    ctx.state.test_plan = {  # type: ignore[attr-defined]
        "ecosystem": plan.ecosystem, "image": plan.image, "setup": plan.setup, "targeted": plan.targeted,
        "full": plan.full, "static": plan.static, "has_tests": plan.has_tests, "source": plan.source,
        "notes": plan.notes, "baseline_includes_new_tests": workspaces.baseline_includes_new_tests,
    }
    ctx.state.setup_outcomes = [p.outcome.value for p in workspaces.setup_results]  # type: ignore[attr-defined]
    return Transition(RunState.RUNNING_TESTS,
                      f"Workspace prepared ({plan.ecosystem}, {len(plan.setup)} setup step(s)); patch applied",
                      event_type="tests_started", message="Patch applied in a disposable workspace; running tests",
                      data={"ecosystem": plan.ecosystem, "sandbox": ctx.deps.sandbox.name})


def _plan_from_state(ctx: NodeContext) -> TestPlan:
    data = getattr(ctx.state, "test_plan", None) or {}
    return TestPlan(ecosystem=data.get("ecosystem", "unknown"), image=data.get("image"),
                    setup=data.get("setup", []), targeted=data.get("targeted", []), full=data.get("full", []),
                    static=data.get("static", []), has_tests=data.get("has_tests", False),
                    source=data.get("source", "conventions"), notes=data.get("notes", []))


async def running_tests(ctx: NodeContext) -> Transition:
    root = tools.workspace_dir(ctx.deps.workspace_root, ctx.run.id)
    if not (root / "patched").is_dir() or not (root / "baseline").is_dir():
        workspaces, plan = await _prepare(ctx)  # recovered after a restart: rebuild the workspace
    else:
        plan = _plan_from_state(ctx)
        workspaces = tools.Workspaces(root, root / "base", root / "baseline", root / "patched",
                                      bool((getattr(ctx.state, "test_plan", None) or {})
                                           .get("baseline_includes_new_tests")), [], {})
    setup_outcomes = getattr(ctx.state, "setup_outcomes", []) or []
    phases: list[PhaseResult] = []
    if all(o == ValidationOutcome.PASSED.value for o in setup_outcomes):
        phases = await tools.run_validation(ctx.deps.sandbox, workspaces, plan, ctx.limits)
    patch_id = ctx.state.current_patch_id
    await ctx.db(lambda s: _record_phases(s, ctx.run.id, patch_id, phases))

    # Setup results are part of the verdict.
    setup_phases = [PhaseResult("setup", [], _placeholder_result(o), ValidationOutcome(o)) for o in setup_outcomes]
    verdict = evaluate(setup_phases + phases, plan)
    static_phases = [p for p in phases if p.phase == "static"]
    # Vacuous truth is not evidence: "static OK" requires at least one check that actually passed.
    static_ok = bool(static_phases) and all(p.outcome == ValidationOutcome.PASSED for p in static_phases)
    v = ctx.state.validation
    v.outcome = verdict.outcome.value
    v.reproduced = verdict.reproduced
    v.regression = verdict.regression
    v.summary = verdict.summary
    v.backend = ctx.deps.sandbox.name
    v.simulated = ctx.deps.sandbox.simulated
    ctx.state.static_ok = static_ok  # type: ignore[attr-defined]
    ctx.state.validation_history.append({
        "attempt": ctx.run.repair_attempt, "patch_id": patch_id, "outcome": verdict.outcome.value,
        "reproduced": verdict.reproduced, "regression": verdict.regression, "summary": verdict.summary[:300]})

    def persist(session: Session) -> None:
        patch = session.get(ProposedPatch, patch_id)
        if patch is not None:
            patch.validation_status = verdict.outcome.value

    await ctx.db(persist)
    label = " (simulated)" if ctx.deps.sandbox.simulated else ""
    return Transition(RunState.EVALUATING_RESULTS, f"Validation {verdict.outcome.value}{label}: {verdict.summary}",
                      event_type="tests_completed",
                      message=f"Tests completed: {verdict.outcome.value}{label}",
                      data={"outcome": verdict.outcome.value, "reproduced": verdict.reproduced,
                            "regression": verdict.regression, "simulated": ctx.deps.sandbox.simulated})


def _placeholder_result(outcome: str) -> Any:
    from app.sandbox.result import ExecutionResult

    return ExecutionResult(exit_code=0 if outcome == ValidationOutcome.PASSED.value else 1, stdout="", stderr="",
                           duration_ms=0, error="sandbox unavailable" if outcome == "not_run" else None)


async def evaluating_results(ctx: NodeContext) -> Transition:
    v = ctx.state.validation
    outcome = ValidationOutcome(v.outcome or ValidationOutcome.NOT_RUN.value)
    patch_id = ctx.state.current_patch_id
    if outcome == ValidationOutcome.FAILED:
        test_output = await ctx.db(lambda s: "\n".join(
            (t.stdout_summary or "")[-3000:] + "\n" + (t.stderr_summary or "")[-1500:]
            for t in s.scalars(select(TestExecution).where(TestExecution.patch_id == patch_id,
                                                           TestExecution.phase.in_(["patched", "static"])))
        ))
        if ctx.run.repair_attempt < ctx.deps.runtime.max_agent_retries:
            patch_text = await ctx.db(lambda s: s.get(ProposedPatch, patch_id).patch_content)  # type: ignore[union-attr]
            system, user = prompts.validation_prompt(ctx.state.plan or {}, patch_text, test_output)
            analysis = await ctx.ask(system=system, user=user, schema=ValidationSummary,
                                     purpose="validation_summary")
            if analysis.retry_recommended:
                return _retry_or_stop(
                    ctx, f"Tests failed after the patch ({v.summary}). Analysis: {analysis.failure_summary} "
                         f"Suggested adjustment: {analysis.suggested_adjustment}", RunOutcome.TESTS_FAILED)
        message = f"Validation failed: {v.summary}"
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.TESTS_FAILED,
                          event_type="needs_human_review", message=message)
    if outcome in {ValidationOutcome.TIMED_OUT, ValidationOutcome.SETUP_FAILED, ValidationOutcome.NOT_RUN}:
        message = f"Validation {outcome.value}: {v.summary}"
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.TESTS_FAILED,
                          event_type="needs_human_review", message=message)

    # PASSED or NOT_AVAILABLE
    ctx.run_updates["diagnosis_status"] = "supported_by_tests" if v.reproduced else "provisional"
    risk = await ctx.db(lambda s: s.get(ProposedPatch, patch_id).risk_level) or "medium"  # type: ignore[union-attr]
    required, reasons = policies.needs_approval(
        ctx.run.autonomy_mode, risk=risk, validation=outcome.value, reproduced=v.reproduced,
        overlapping_changes=False, auto_pr_enabled=ctx.deps.settings.auto_create_pull_requests,
    )
    if not required:
        return Transition(RunState.READY_TO_PUBLISH, "Policy permits automatic publication (Mode C)",
                          event_type="ready_to_publish", message="Validated patch ready to publish")

    def create_approval(session: Session) -> int:
        approval = session.scalar(select(Approval).where(
            Approval.run_id == ctx.run.id, Approval.patch_id == patch_id,
            Approval.approval_type == ApprovalType.PUBLISH_PULL_REQUEST.value))
        if approval is None:
            approval = Approval(run_id=ctx.run.id, patch_id=patch_id,
                                approval_type=ApprovalType.PUBLISH_PULL_REQUEST.value,
                                status=ApprovalStatus.PENDING.value, is_demo=ctx.run.is_demo,
                                risk_summary_json={"risk": risk, "reasons": reasons, "validation": outcome.value,
                                                   "reproduced": v.reproduced})
            session.add(approval)
            session.flush()
        return approval.id

    ctx.state.approval_id = await ctx.db(create_approval)
    return Transition(RunState.WAITING_FOR_APPROVAL, "Waiting for human approval: " + " ".join(reasons),
                      RunOutcome.AWAITING_APPROVAL, event_type="approval_required",
                      message="Human approval required before publication",
                      data={"approval_id": ctx.state.approval_id, "reasons": reasons})


# --- READY_TO_PUBLISH / PUBLISHING_BRANCH / CREATING_PULL_REQUEST ---------------------------

async def ready_to_publish(ctx: NodeContext) -> Transition:
    assert ctx.state.repository and ctx.state.revision
    patch_id = ctx.state.current_patch_id

    def load(session: Session) -> tuple[bool, bool, str]:
        repo = session.get(Repository, ctx.state.repository.id)  # type: ignore[union-attr]
        approved = session.scalar(select(Approval).where(
            Approval.run_id == ctx.run.id, Approval.patch_id == patch_id,
            Approval.status == ApprovalStatus.APPROVED.value)) is not None
        patch = session.get(ProposedPatch, patch_id)
        assert patch is not None
        return bool(repo and repo.enabled), approved, patch.patch_content

    repo_enabled, approved, patch_text = await ctx.db(load)
    if ctx.run.autonomy_mode != AutonomyMode.AUTO_PR and not approved:
        raise PolicyViolation("Publication requires an approved approval record.")
    # Re-run every policy check against the *current* settings before publishing.
    tree_paths, symlinks, submodules, _ = await ctx.tree_sets()
    runtime = ctx.deps.runtime
    report = validate_patch(patch_text, tools.patch_policy(
        max_files=runtime.max_patch_files, max_lines=runtime.max_patch_lines, protected=runtime.protected_paths,
        secrets=ctx.deps.known_secrets, investigation_tree=tree_paths, symlinks=symlinks, submodules=submodules))
    v = ctx.state.validation
    blockers = policies.publication_blockers(
        validation=v.outcome or "", static_ok=bool(getattr(ctx.state, "static_ok", False)),
        has_human_approval=approved, repository_enabled=repo_enabled, policy_blocked=report.blocked,
        sandbox_simulated=v.simulated, is_demo=ctx.run.is_demo)
    auto_pr_disabled = not ctx.deps.settings.auto_create_pull_requests
    if ctx.run.autonomy_mode == AutonomyMode.AUTO_PR and not approved and auto_pr_disabled:
        blockers.append("AUTO_CREATE_PULL_REQUESTS is disabled.")
    if blockers:
        message = "Publication blocked: " + " ".join(blockers)
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.POLICY_BLOCKED,
                          event_type="needs_human_review", message=truncate(message, 900))
    host = ctx.deps.require_code_host()
    full_name = ctx.state.repository.full_name
    await host.get_commit(full_name, ctx.state.revision)  # base commit must still exist
    head = await host.get_branch_head(full_name, ctx.state.repository.base_branch)
    if head is None:
        raise PolicyViolation(f"Base branch {ctx.state.repository.base_branch} no longer exists.")
    overlapping: list[str] = []
    if head != ctx.state.revision:
        changed = await host.changed_files_between(full_name, ctx.state.revision, head)
        overlapping = sorted(set(changed) & set(report.paths))
    ctx.state.publish.base_branch_head = head
    ctx.state.publish.overlapping_changes = overlapping
    if overlapping and not approved:
        message = f"Files in the patch changed on {ctx.state.repository.base_branch} since analysis: {overlapping}"
        return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.POLICY_BLOCKED,
                          event_type="needs_human_review", message=message)
    return Transition(RunState.PUBLISHING_BRANCH, "Pre-publication checks passed"
                      + (f" (warning: {len(overlapping)} file(s) changed upstream)" if overlapping else ""))


async def publishing_branch(ctx: NodeContext) -> Transition:
    assert ctx.state.repository and ctx.state.revision
    host = ctx.deps.require_code_host()
    full_name = ctx.state.repository.full_name
    patch_id = ctx.state.current_patch_id
    assert patch_id is not None
    trailer = tools.commit_trailer(ctx.run.id, patch_id)
    publish = ctx.state.publish

    async def ours(branch: str) -> str | None:
        sha = await host.get_branch_head(full_name, branch)
        if sha is None:
            return None
        commit = await host.get_commit(full_name, sha)
        return sha if trailer in commit.message else "foreign"

    # Recovery: a previous attempt may have created the branch before the process stopped.
    if publish.branch:
        existing = await ours(publish.branch)
        if existing and existing != "foreign":
            publish.head_sha = existing
            return Transition(RunState.CREATING_PULL_REQUEST, f"Branch {publish.branch} already published")

    patch_text = await ctx.db(lambda s: s.get(ProposedPatch, patch_id).patch_content)  # type: ignore[union-attr]
    files = parse_unified_diff(patch_text)
    originals = await ctx.file_texts([f.path for f in files])
    new_contents = apply_patch_to_texts(originals, files)
    _, _, _, modes = await ctx.tree_sets()
    issue_title = truncate((ctx.state.issue or {}).get("title"), 120)
    message = (f"fix: {issue_title}\n\nGenerated by Patchora for Sentry issue {ctx.run.sentry_issue_id}."
               f"\n\n{trailer}")

    for suffix in range(5):
        branch = tools.branch_name_for(ctx.run.sentry_issue_id, ctx.run.id, suffix)
        existing = await ours(branch)
        if existing == "foreign":
            continue  # never overwrite a branch we did not create
        if existing:
            publish.branch, publish.head_sha = branch, existing
            break
        publish.branch = branch
        await ctx.db(lambda s: _save_state_field(s, ctx.run.id, ctx.state))  # durable intent before the write
        try:
            sha = await host.create_branch_with_commit(
                full_name, base_sha=ctx.state.revision, branch=branch,
                default_branch=ctx.state.repository.default_branch, files=new_contents,
                file_modes={p: modes.get(p, "100644") for p in new_contents}, message=message)
        except ConflictError:
            existing = await ours(branch)
            if existing and existing != "foreign":
                publish.head_sha = existing
                break
            continue
        publish.head_sha = sha
        break
    else:
        raise PolicyViolation("Could not find an unused branch name for this run.")

    def persist(session: Session) -> None:
        patch = session.get(ProposedPatch, patch_id)
        if patch is not None:
            patch.branch_name = publish.branch
        audit_service.record(session, actor_type="agent", actor_id=f"run:{ctx.run.id}", action="branch.published",
                             resource_type="analysis_run", resource_id=ctx.run.id,
                             details={"repository": full_name, "branch": publish.branch, "sha": publish.head_sha,
                                      "simulated": host.simulated})

    await ctx.db(persist)
    label = " (simulated, not pushed to GitHub)" if host.simulated else ""
    return Transition(RunState.CREATING_PULL_REQUEST, f"Published {publish.branch}{label}",
                      event_type="branch_published", message=f"Branch published: {publish.branch}{label}",
                      data={"branch": publish.branch, "simulated": host.simulated})


def _save_state_field(session: Session, run_id: int, state: AgentState) -> None:
    run = session.get(AnalysisRun, run_id)
    if run is not None:
        run.state_json = state.model_dump(mode="json")


async def creating_pull_request(ctx: NodeContext) -> Transition:
    assert ctx.state.repository and ctx.state.publish.branch
    host = ctx.deps.require_code_host()
    full_name = ctx.state.repository.full_name
    patch_id = ctx.state.current_patch_id

    existing_row = await ctx.db(lambda s: s.scalar(select(PullRequest).where(PullRequest.run_id == ctx.run.id)))
    if existing_row is not None:
        ctx.state.publish.pr_url = existing_row.url
        ctx.state.publish.pr_number = existing_row.external_pr_number
        return Transition(RunState.COMPLETED, f"Pull request already recorded: {existing_row.url}",
                          RunOutcome.PULL_REQUEST_CREATED, event_type="run_completed", message="Run completed")

    def gather(session: Session) -> dict[str, Any]:
        patch = session.get(ProposedPatch, patch_id)
        assert patch is not None
        tests = [{"command": t.command_summary.split(" "), "phase": t.phase, "status": t.status,
                  "passed": t.tests_passed, "failed": t.tests_failed}
                 for t in session.scalars(select(TestExecution).where(TestExecution.patch_id == patch_id))]
        gh_issue = session.scalar(select(GitHubIssue).where(GitHubIssue.sentry_issue_id == ctx.run.issue_pk))
        issue = session.get(SentryIssue, ctx.run.issue_pk)
        run = session.get(AnalysisRun, ctx.run.id)
        return {"files": patch.files_changed_json, "summary": patch.diff_summary or "",
                "findings": patch.policy_findings_json, "tests": tests,
                "gh_issue": gh_issue.url if gh_issue else None, "permalink": issue.permalink if issue else None,
                "diagnosis_status": run.diagnosis_status if run else None,
                "llm_simulated": bool(run and run.llm_simulated)}

    info = await ctx.db(gather)
    simulated = [name for name, flag in (("model", info["llm_simulated"]),
                                         ("tests", ctx.state.validation.simulated),
                                         ("GitHub publication", host.simulated)) if flag]
    if ctx.state.publish.overlapping_changes:
        ctx.state.add_note(f"Upstream changes to {ctx.state.publish.overlapping_changes} since analysis; "
                           "rebase may be required.")
    body = tools.render_pr_body(
        issue=ctx.state.issue or {}, permalink=info["permalink"], diagnosis=ctx.state.diagnosis,
        plan=ctx.state.plan, evidence=(ctx.state.investigation or {}).get("evidence", []), files=info["files"],
        diff_summary=info["summary"], validation=ctx.state.validation.model_dump(), test_runs=info["tests"],
        policy_findings=info["findings"], revision_note=ctx.state.revision_note or "",
        diagnosis_status=info["diagnosis_status"] or "provisional", github_issue_url=info["gh_issue"],
        simulated=simulated, run_id=ctx.run.id,
    )
    summary = ((ctx.state.diagnosis or {}).get("issue_summary") or {}).get("error_type") or "error"
    title = f"[ Patchora ] Fix: {truncate((ctx.state.issue or {}).get('title') or summary, 150)}"
    pr = await host.find_pull_request(full_name, ctx.state.publish.branch)
    if pr is None:
        pr = await host.create_pull_request(full_name, title=title, body=body, head=ctx.state.publish.branch,
                                            base=ctx.state.repository.base_branch,
                                            draft=ctx.deps.runtime.pr_draft)
    ctx.state.publish.pr_number = pr.number
    ctx.state.publish.pr_url = pr.url

    def persist(session: Session) -> None:
        if session.scalar(select(PullRequest).where(PullRequest.run_id == ctx.run.id)) is None:
            session.add(PullRequest(
                run_id=ctx.run.id, repository_id=ctx.state.repository.id,  # type: ignore[union-attr]
                external_pr_number=pr.number, external_pr_id=pr.id, title=title, branch_name=pr.head_branch,
                base_branch=pr.base_branch, head_commit_sha=ctx.state.publish.head_sha, url=pr.url,
                status="open", draft=pr.draft, validation_outcome=ctx.state.validation.outcome,
                is_demo=ctx.run.is_demo))
        audit_service.record(session, actor_type="agent", actor_id=f"run:{ctx.run.id}",
                             action="pull_request.created", resource_type="analysis_run", resource_id=ctx.run.id,
                             details={"url": pr.url, "number": pr.number, "simulated": host.simulated})

    await ctx.db(persist)
    label = " (simulated - not created on GitHub)" if host.simulated else ""
    return Transition(RunState.COMPLETED, f"Pull request #{pr.number}{label}: {pr.url}",
                      RunOutcome.PULL_REQUEST_CREATED, event_type="pull_request_created",
                      message=f"Pull request created{label}", data={"url": pr.url, "number": pr.number})


HANDLERS: dict[RunState, Handler] = {
    RunState.RECEIVED: received,
    RunState.VALIDATING: validating,
    RunState.DEDUPLICATING: deduplicating,
    RunState.FETCHING_SENTRY_DATA: fetching_sentry_data,
    RunState.MAPPING_REPOSITORY: mapping_repository,
    RunState.FETCHING_SOURCE: fetching_source,
    RunState.INVESTIGATING: investigating,
    RunState.DIAGNOSIS_READY: diagnosis_ready,
    RunState.PLANNING_PATCH: planning_patch,
    RunState.GENERATING_PATCH: generating_patch,
    RunState.REVIEWING_PATCH: reviewing_patch,
    RunState.APPLYING_PATCH: applying_patch,
    RunState.RUNNING_TESTS: running_tests,
    RunState.EVALUATING_RESULTS: evaluating_results,
    RunState.READY_TO_PUBLISH: ready_to_publish,
    RunState.PUBLISHING_BRANCH: publishing_branch,
    RunState.CREATING_PULL_REQUEST: creating_pull_request,
}

STEP_NAMES: dict[RunState, str] = {
    RunState.RECEIVED: "start_run",
    RunState.VALIDATING: "validate_request",
    RunState.DEDUPLICATING: "deduplicate",
    RunState.FETCHING_SENTRY_DATA: "fetch_sentry_issue",
    RunState.MAPPING_REPOSITORY: "resolve_repository_and_revision",
    RunState.FETCHING_SOURCE: "retrieve_source_evidence",
    RunState.INVESTIGATING: "generate_root_cause_hypothesis",
    RunState.DIAGNOSIS_READY: "check_evidence_sufficiency",
    RunState.PLANNING_PATCH: "build_patch_plan",
    RunState.GENERATING_PATCH: "generate_structured_patch",
    RunState.REVIEWING_PATCH: "validate_patch_scope_and_policy",
    RunState.APPLYING_PATCH: "apply_patch_in_workspace",
    RunState.RUNNING_TESTS: "run_tests",
    RunState.EVALUATING_RESULTS: "evaluate_results",
    RunState.READY_TO_PUBLISH: "recheck_policy_before_publication",
    RunState.PUBLISHING_BRANCH: "publish_branch",
    RunState.CREATING_PULL_REQUEST: "create_pull_request",
}


def cost_for(ctx: NodeContext) -> float | None:
    s = ctx.deps.settings
    # Pricing is only configured for Groq (GROQ_PRICE_*); other providers report no cost rather than a guess.
    if getattr(ctx.deps.llm, "provider", "groq") != "groq":
        return None
    return estimate_cost(ctx.usage["prompt"], ctx.usage["completion"], s.groq_price_input_per_million,
                         s.groq_price_output_per_million)
