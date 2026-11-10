"""Evaluation harness.

Runs every dataset case through the real :class:`WorkflowEngine` against an isolated,
temporary SQLite database (real runs are never touched), then computes metrics from what
actually happened and writes a JSON + Markdown report.

Usage::

    python -m app.evaluation.evaluator --llm scripted --sandbox auto
    python -m app.evaluation.evaluator --llm groq            # uses GROQ_API_KEY / GROQ_MODEL
    python -m app.evaluation.evaluator --llm active          # the provider selected on the Integrations page

* ``--llm scripted``: deterministic, pre-recorded model responses. Measures the pipeline
  (evidence gathering, gates, validation, publication, idempotency), NOT model quality.
* ``--llm groq`` / ``--llm active``: the configured Groq model, or the active provider (Groq, Anthropic,
  OpenAI, DeepSeek, Gemini or AWS Bedrock), answers every prompt. Cases marked
  ``scripted_only`` (malformed output, forced timeout) are skipped.
* Code is executed for real on the bundled synthetic fixtures: in Docker when available,
  otherwise with the local runner (acceptable only because the fixtures are trusted,
  bundled test code; this is stated in the report).
* GitHub publication is simulated (local fixture code host); nothing leaves the machine.
* Reviewer approval, where a case requires it, is simulated by the harness and labeled.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.graph import WorkflowEngine
from app.agent.prompts import PROMPT_VERSION
from app.config import Settings, get_settings
from app.core.enums import RunState, StepStatus, UserRole
from app.core.exceptions import ExternalAuthError
from app.db.migrate import upgrade_to_head
from app.db.models import (
    AgentStep,
    AnalysisRun,
    Approval,
    EvaluationRun,
    ProposedPatch,
    PullRequest,
    Repository,
    RepositoryMapping,
    SentryIssue,
)
from app.db.session import build_engine, init_engine, session_scope
from app.demo.providers import DemoCodeHost
from app.dependencies import ensure_local_user
from app.evaluation.dataset import DATASET_VERSION, EVAL_SHA, EvalCase, build_cases
from app.evaluation.metrics import compute_metrics
from app.providers.factory import ProviderFactory
from app.providers.llm import LLMClient, ScriptedLLM
from app.sandbox.docker_runner import DockerSandbox
from app.sandbox.interface import Sandbox
from app.sandbox.local_runner import LocalSandbox
from app.schemas.issues import NormalizedEvent, NormalizedIssue, ReleaseCommit
from app.schemas.repositories import TreeEntry
from app.security.authorization import Principal
from app.services import approval_service, run_service
from app.services.patch_service import is_test_path
from app.services.sentry_service import parse_event, parse_issue
from app.services.settings_service import get_runtime_settings, update_runtime_settings

EVAL_ORG = "acme-eval"
MANIFESTS = {"requirements.txt", "package.json", "pyproject.toml", "README.md", "pytest.ini"}


class EvalIssueSource:
    simulated = True
    organization = EVAL_ORG

    def __init__(self, case: EvalCase) -> None:
        self.case = case

    async def list_issues(self, project: str, **_: Any) -> tuple[list[NormalizedIssue], str | None]:
        return [parse_issue(self.case.issue, EVAL_ORG)], None

    async def get_issue(self, issue_id: str) -> NormalizedIssue:
        return parse_issue(self.case.issue, EVAL_ORG)

    async def get_latest_event(self, issue_id: str) -> NormalizedEvent:
        return parse_event(self.case.event)

    async def get_release_commits(self, version: str) -> list[ReleaseCommit]:
        return [ReleaseCommit(sha=EVAL_SHA, repository=self.case.repo)]


class FaultyCodeHost(DemoCodeHost):
    async def get_tree(self, full_name: str, sha: str) -> list[TreeEntry]:
        raise ExternalAuthError("GitHub API returned 403: Resource not accessible by integration",
                                service="github", upstream_status=403)


def choose_sandbox(kind: str, settings: Settings) -> Sandbox:
    docker = DockerSandbox(settings.sandbox_python_image)
    if kind == "docker":
        return docker
    if kind == "local":
        return LocalSandbox(enabled=True)
    ok, _ = docker._probe()
    return docker if ok else LocalSandbox(enabled=True)


class Harness:
    def __init__(self, settings: Settings, llm_mode: str, sandbox: Sandbox, groq: LLMClient | None) -> None:
        self.settings = settings
        self.llm_mode = llm_mode
        self.sandbox = sandbox
        self.groq = groq
        self.case: EvalCase | None = None
        self.factory = ProviderFactory(settings)
        self.factory.overrides["issue_source"] = lambda s, r: EvalIssueSource(self._case())
        self.factory.overrides["code_host"] = self._code_host
        self.factory.overrides["llm"] = self._llm
        self.factory.overrides["sandbox"] = lambda s, r: self.sandbox
        self.engine = WorkflowEngine(self.factory)
        self.issue_rows: dict[str, int] = {}

    def _case(self) -> EvalCase:
        assert self.case is not None
        return self.case

    def _code_host(self, session: Session, run: AnalysisRun | None) -> DemoCodeHost:
        case = self._case()
        cls = FaultyCodeHost if case.code_host_fault else DemoCodeHost
        return cls(root=case.fixture, full_name=case.repo, sha=EVAL_SHA)

    def _llm(self, session: Session, run: AnalysisRun | None) -> LLMClient:
        if self.llm_mode in {"groq", "active"}:
            assert self.groq is not None
            return self.groq
        responses = json.loads(json.dumps(self._case().responses))
        return ScriptedLLM(responses=responses, model_name="scripted-eval-model")

    def _seed(self, case: EvalCase) -> int:
        with session_scope() as s:
            owner, name = case.repo.split("/")
            repo = s.scalar(select(Repository).where(Repository.full_name == case.repo))
            if repo is None:
                repo = Repository(provider="evaluation", provider_repository_id=case.repo, owner=owner, name=name,
                                  full_name=case.repo, default_branch="main", enabled=True)
                s.add(repo)
                s.flush()
            if run_service.find_mapping(s, EVAL_ORG, case.project) is None:
                s.add(RepositoryMapping(sentry_organization=EVAL_ORG, sentry_project=case.project,
                                        repository_id=repo.id, strip_prefixes_json=case.strip_prefixes,
                                        path_prefixes_json=case.path_prefixes))
                s.flush()
            if case.reuse_issue_of:
                return self.issue_rows[case.reuse_issue_of]
            issue = dict(case.issue)
            issue["project"] = {"slug": case.project}
            row = run_service.upsert_issue(s, parse_issue(issue, EVAL_ORG))
            self.issue_rows[case.key] = row.id
            return row.id

    async def run_case(self, case: EvalCase) -> dict[str, Any]:
        if case.scripted_only and self.llm_mode != "scripted":
            return {"key": case.key, "category": case.category, "skipped": True,
                    "reason": "scripted-only case (requires controlled model output)"}
        self.case = case
        issue_pk = self._seed(case)
        with session_scope() as s:
            timeout = case.test_timeout_seconds or self.settings.max_test_runtime_seconds
            update_runtime_settings(s, {"max_test_runtime_seconds": timeout}, "evaluation", self.settings)
            issue = s.get(SentryIssue, issue_pk)
            assert issue is not None
            run, _ = run_service.create_run(s, issue, trigger="evaluation", actor_type="system",
                                            actor_id="evaluation", autonomy_mode=case.mode)
            run_id = run.id
        started = time.monotonic()
        state = await self.engine.execute(run_id)
        approved = False
        if state == RunState.WAITING_FOR_APPROVAL and case.approve:
            with session_scope() as s:
                approval = s.scalar(select(Approval).where(Approval.run_id == run_id))
                assert approval is not None
                reviewer = ensure_local_user(s)  # the harness acts as the (simulated) human reviewer
                principal = Principal(user_id=reviewer.id, external_id="evaluation-harness",
                                      display_name="Evaluation harness", role=UserRole.REVIEWER)
                approval_service.approve(s, approval.id, principal, "approval simulated by evaluation harness")
            approved = True
            state = await self.engine.execute(run_id)
        duration = int((time.monotonic() - started) * 1000)
        return self._collect(case, run_id, duration, approved)

    def _collect(self, case: EvalCase, run_id: int, duration: int, approved: bool) -> dict[str, Any]:
        with session_scope() as s:
            run = s.get(AnalysisRun, run_id)
            assert run is not None
            state = run.state_json or {}
            evidence = (state.get("investigation") or {}).get("evidence", [])
            source_paths = {e["path"] for e in evidence if not is_test_path(e["path"]) and e["path"] not in MANIFESTS}
            precision = recall = None
            if case.relevant_files and state.get("investigation") is not None:
                hits = source_paths & case.relevant_files
                precision = round(len(hits) / len(source_paths), 3) if source_paths else 0.0
                recall = round(len({p for p in case.relevant_files if p in {e["path"] for e in evidence}})
                               / len(case.relevant_files), 3)
            diagnosis = state.get("diagnosis") or {}
            top = (diagnosis.get("hypotheses") or [{}])[0]
            cited = {e["path"] for e in evidence if e["id"] in set(top.get("evidence_ids", []))}
            patches = s.scalars(select(ProposedPatch).where(ProposedPatch.run_id == run_id)).all()
            applied = s.scalar(select(AgentStep).where(AgentStep.run_id == run_id,
                                                       AgentStep.state == RunState.APPLYING_PATCH.value,
                                                       AgentStep.status == StepStatus.SUCCEEDED.value)) is not None
            history = state.get("validation_history") or []
            pr = s.scalar(select(PullRequest).where(PullRequest.run_id == run_id))
            outcome = run.outcome or run.status.lower()
            return {
                "key": case.key, "category": case.category, "description": case.description, "skipped": False,
                "status": run.status, "outcome": outcome,
                "expected_outcomes": sorted(case.expected_outcomes),
                "outcome_correct": outcome in case.expected_outcomes,
                "expect_repository": case.expect_repository,
                "repository_resolved": bool(state.get("repository")) and bool(run.source_revision),
                "retrieved_source_files": sorted(source_paths),
                "retrieval_precision": precision, "retrieval_recall": recall,
                "root_cause_expected": case.category == "code_defect",
                "root_cause_cites_relevant_file": bool(cited & case.relevant_files),
                "classification": diagnosis.get("error_classification"),
                "confidence": run.confidence_score,
                "patch_generated": bool(patches), "patch_applied": applied,
                "final_validation": (state.get("validation") or {}).get("outcome"),
                "validation_history": history,
                "regressions_detected": sum(1 for h in history if h.get("regression")),
                "final_regression": bool(history and history[-1].get("regression")),
                "expect_abstain": case.expect_abstain,
                "abstained": outcome.startswith("abstained"),
                "expect_pull_request": case.expect_pull_request,
                "pull_request_created": pr is not None,
                "approval_simulated": approved,
                "repair_attempts": run.repair_attempt,
                "duration_ms": duration,
                "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens,
                "estimated_cost": run.estimated_cost,
                "model": run.model_name, "error": run.error_message,
            }


def _config_version(settings: Settings) -> str:
    with session_scope() as s:
        runtime = get_runtime_settings(s, settings).model_dump(mode="json")
    runtime.pop("max_test_runtime_seconds", None)
    blob = json.dumps({"runtime": runtime, "prompt_version": PROMPT_VERSION}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _human_acceptance(main_url: str) -> dict[str, Any]:
    """Real reviewer decisions from the main database (non-demo); None when there are none."""
    try:
        engine = build_engine(main_url)
        with Session(engine) as session:
            rows = session.scalars(select(Approval).where(Approval.is_demo.is_(False))).all()
        engine.dispose()
    except Exception:
        return {"human_acceptance_rate": None, "human_decisions": 0}
    decided = [r for r in rows if r.status in {"approved", "rejected"}]
    approved = sum(1 for r in decided if r.status == "approved")
    return {"human_acceptance_rate": round(approved / len(decided), 3) if decided else None,
            "human_decisions": len(decided)}


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        f"# Patchora evaluation report ({report['started_at']})",
        "",
        f"* Dataset version: `{report['dataset_version']}` ({m['cases_evaluated']} evaluated, "
        f"{m['cases_skipped']} skipped)",
        f"* LLM mode: `{report['llm_mode']}` - model `{report['model_name']}`",
        f"* Prompt version: `{report['prompt_version']}` - config version `{report['config_version']}`",
        f"* Sandbox: `{report['sandbox']}` - GitHub publication: simulated (local fixture host)",
        "",
        "> " + report["interpretation"],
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    lines += [f"| {k} | {v if v is not None else 'n/a'} |" for k, v in m.items()]
    lines += ["", "## Cases", "", "| Case | Category | Outcome | Expected | Correct | Validation | Repairs | ms |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in report["results"]:
        if r.get("skipped"):
            lines.append(f"| {r['key']} | {r['category']} | skipped | - | - | - | - | - |")
            continue
        lines.append(f"| {r['key']} | {r['category']} | {r['outcome']} | {', '.join(r['expected_outcomes'])} | "
                     f"{'yes' if r['outcome_correct'] else 'NO'} | {r['final_validation'] or '-'} | "
                     f"{r['repair_attempts']} | {r['duration_ms']} |")
    return "\n".join(lines) + "\n"


async def evaluate(llm_mode: str, sandbox_kind: str, only: list[str] | None, output: Path) -> dict[str, Any]:
    main_settings = get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work = main_settings.data_dir / "eval" / stamp
    work.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{(work / 'eval.db').as_posix()}"
    settings = main_settings.model_copy(update={
        "database_url": db_url, "data_dir": work, "auto_create_pull_requests": True, "faiss_enabled": False})
    upgrade_to_head(db_url)
    init_engine(db_url)

    groq: LLMClient | None = None
    model_name = "scripted-eval-model"
    if llm_mode in {"groq", "active"}:
        # Provider credentials and model are configured on the Integrations page (main database).
        upgrade_to_head(main_settings.database_url)
        main_engine = build_engine(main_settings.database_url)
        try:
            with Session(main_engine) as main_session:
                factory = ProviderFactory(main_settings)
                runtime = get_runtime_settings(main_session, main_settings)
                groq = factory.groq(main_session, runtime) if llm_mode == "groq" \
                    else factory.llm(main_session, runtime)
        finally:
            main_engine.dispose()
        model_name = groq.model_name
    sandbox = choose_sandbox(sandbox_kind, settings)
    harness = Harness(settings, llm_mode, sandbox, groq)

    started = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    for case in build_cases():
        if only and case.key not in only:
            continue
        result = await harness.run_case(case)
        results.append(result)
        status = "skipped" if result.get("skipped") else ("ok" if result["outcome_correct"] else "MISMATCH")
        print(f"  {case.key:32s} {status:9s} {result.get('outcome', '')}", flush=True)

    metrics = compute_metrics(results) | _human_acceptance(main_settings.database_url)
    sandbox_label = sandbox.name + (" (local runner on bundled, trusted synthetic fixtures)"
                                    if sandbox.name == "local" else "")
    report = {
        "dataset_version": DATASET_VERSION, "llm_mode": llm_mode, "model_name": model_name,
        "prompt_version": PROMPT_VERSION, "config_version": _config_version(settings), "sandbox": sandbox_label,
        "started_at": started.isoformat(), "completed_at": datetime.now(UTC).isoformat(),
        "interpretation": ("Scripted mode replays fixed model responses: these numbers measure the pipeline "
                           "(evidence, gates, sandboxed validation, publication, idempotency), not model quality."
                           if llm_mode == "scripted" else
                           f"Model mode ({getattr(groq, 'label', 'LLM')}): every diagnosis, plan and patch was "
                           "produced by the configured model."),
        "metrics": metrics, "results": results,
    }
    await asyncio.to_thread(write_report, output, stamp, report)
    _record(main_settings, report)
    return report


def write_report(output: Path, stamp: str, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / f"eval-{stamp}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (output / f"eval-{stamp}.md").write_text(render_markdown(report), encoding="utf-8")


def _record(main_settings: Settings, report: dict[str, Any]) -> None:
    try:
        upgrade_to_head(main_settings.database_url)
        engine = build_engine(main_settings.database_url)
        with Session(engine) as session:
            session.add(EvaluationRun(
                dataset_version=report["dataset_version"], model_name=report["model_name"],
                llm_mode=report["llm_mode"], config_version=report["config_version"], status="completed",
                metrics_json=report["metrics"], results_json=report["results"],
                started_at=datetime.fromisoformat(report["started_at"]),
                completed_at=datetime.fromisoformat(report["completed_at"])))
            session.commit()
        engine.dispose()
    except Exception as exc:  # the report files are still written
        print(f"warning: could not record the evaluation in the main database: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Patchora evaluation suite.")
    parser.add_argument("--llm", choices=["scripted", "groq", "active"], default="scripted")
    parser.add_argument("--sandbox", choices=["auto", "docker", "local"], default="auto")
    parser.add_argument("--case", action="append", help="Run only this case (repeatable)")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    output = args.output or get_settings().data_dir / "eval_reports"
    print(f"Patchora evaluation - dataset {DATASET_VERSION}, llm={args.llm}, sandbox={args.sandbox}")
    report = asyncio.run(evaluate(args.llm, args.sandbox, args.case, output))
    print(json.dumps(report["metrics"], indent=2))
    print(f"Report written to {output}")


if __name__ == "__main__":
    main()
