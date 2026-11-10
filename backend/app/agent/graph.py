"""Durable workflow engine.

Why a custom engine instead of LangGraph: the workflow must survive process restarts with
its state in SQLite, resume from the last completed step, never repeat a non-idempotent
GitHub action without checking first, and enforce an explicit transition table. Those
requirements are simpler to guarantee and test with a small, explicit state machine than
by adapting a general graph runtime and its checkpointer. Model calls are isolated in
:class:`app.providers.llm.LLMClient`, so swapping in another orchestrator later only affects
this module.

Execution model:
* The engine loads the run, executes the handler for its current state and commits the
  resulting transition, step record, state and events in **one** short transaction.
* No database transaction is open while a handler awaits external services.
* Retryable external failures propagate to the job runner, which re-schedules the job with
  backoff; the run stays in its current state so the step is retried on resume.
* Cancellation is cooperative and checked between steps.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import select

from app.agent.nodes import HANDLERS, STEP_NAMES, NodeContext, RunSnapshot, Transition, cost_for
from app.agent.state import AgentState
from app.agent.tools import cleanup_workspace
from app.agent.transitions import InvalidTransition, is_cancellable, validate_transition
from app.config import AutonomyMode
from app.core.enums import PARKED_STATES, TERMINAL_STATES, RunOutcome, RunState, StepStatus
from app.core.exceptions import (
    CancelledByUser,
    ExternalAuthError,
    ExternalNotFound,
    ExternalServiceError,
    LLMOutputError,
    NotConfiguredError,
    PolicyViolation,
)
from app.core.logging import get_logger, redact
from app.db.base import utcnow
from app.db.models import AgentStep, AnalysisRun, ProposedPatch, PullRequest, SentryIssue
from app.db.session import session_scope, with_write_retry
from app.providers.factory import ProviderFactory, RunDependencies
from app.schemas.agent_outputs import FinalRepairReport
from app.security.input_sanitization import truncate
from app.services import audit_service

logger = get_logger(__name__)


class WorkflowEngine:
    def __init__(self, factory: ProviderFactory) -> None:
        self.factory = factory

    # --- loading ---------------------------------------------------------------------------

    @staticmethod
    def _load(run_id: int) -> tuple[RunSnapshot, AgentState, bool] | None:
        with session_scope() as session:
            run = session.get(AnalysisRun, run_id)
            if run is None:
                return None
            issue = session.get(SentryIssue, run.sentry_issue_id)
            assert issue is not None
            snapshot = RunSnapshot(
                id=run.id, status=run.status, autonomy_mode=AutonomyMode(run.autonomy_mode),
                is_demo=run.is_demo, trigger=run.trigger, issue_pk=issue.id,
                sentry_issue_id=issue.sentry_issue_id, organization=issue.organization, project=issue.project,
                repository_id=run.repository_id, repair_attempt=run.repair_attempt,
                source_revision=run.source_revision,
            )
            return snapshot, AgentState.model_validate(run.state_json or {}), run.cancel_requested

    @staticmethod
    def _start_step(run_id: int, state: RunState) -> int:
        def _do(session: Any) -> int:
            last = session.scalar(select(AgentStep.sequence_number).where(AgentStep.run_id == run_id)
                                  .order_by(AgentStep.sequence_number.desc()).limit(1)) or 0
            prior = session.scalar(select(AgentStep).where(AgentStep.run_id == run_id,
                                                           AgentStep.state == state.value)
                                   .order_by(AgentStep.sequence_number.desc()).limit(1))
            step = AgentStep(run_id=run_id, sequence_number=last + 1, step_type=STEP_NAMES[state],
                             state=state.value, status=StepStatus.RUNNING.value,
                             retry_count=(prior.retry_count + 1) if prior is not None else 0)
            session.add(step)
            run = session.get(AnalysisRun, run_id)
            if run is not None:
                run.current_step = STEP_NAMES[state]
            session.flush()
            return int(step.id)

        return with_write_retry(_do)

    # --- committing ------------------------------------------------------------------------

    def _commit(self, run_id: int, step_id: int, from_state: RunState, ctx: NodeContext,
                transition: Transition, started: float) -> RunState:
        duration = int((time.monotonic() - started) * 1000)

        def _do(session: Any) -> RunState:
            run = session.get(AnalysisRun, run_id)
            step = session.get(AgentStep, step_id)
            assert run is not None and step is not None
            if run.status != from_state.value:
                # Changed underneath us (e.g. cancelled while parked). Do not overwrite.
                step.status = StepStatus.SKIPPED.value
                step.completed_at = utcnow()
                return RunState(run.status)
            target = validate_transition(from_state, transition.target)
            failed = target in {RunState.FAILED}
            step.status = StepStatus.FAILED.value if failed else StepStatus.SUCCEEDED.value
            step.output_summary = truncate(redact(transition.summary), 2000)
            step.error_message = truncate(redact(transition.error or ""), 2000) or None
            step.duration_ms = duration
            step.completed_at = utcnow()
            step.metadata_json = {**ctx.step_meta, "tokens": {k: v for k, v in ctx.usage.items()}}
            step.input_summary = truncate(f"state={from_state.value}", 500)

            updates = dict(ctx.run_updates)
            if updates.pop("started_at", False) and run.started_at is None:
                run.started_at = utcnow()
            for key, value in updates.items():
                setattr(run, key, value)
            run.status = target.value
            run.current_step = STEP_NAMES.get(target, target.value.lower())
            if transition.outcome is not None:
                run.outcome = transition.outcome.value
            if transition.error:
                run.error_message = truncate(redact(transition.error), 2000)
            run.prompt_tokens += int(ctx.usage.get("prompt", 0))
            run.completion_tokens += int(ctx.usage.get("completion", 0))
            if ctx.usage.get("model"):
                run.model_name = ctx.usage["model"]
                run.llm_simulated = bool(ctx.usage.get("simulated"))
            cost = cost_for(ctx)
            if cost is not None:
                run.estimated_cost = round((run.estimated_cost or 0) + cost, 6)
            if target in TERMINAL_STATES:
                run.completed_at = utcnow()
                if run.started_at is not None:
                    run.total_duration_ms = int((run.completed_at - run.started_at).total_seconds() * 1000)
                ctx.state.final_report = self._final_report(session, run, ctx.state).model_dump()  # type: ignore[attr-defined]
                audit_service.record(session, actor_type="agent", actor_id=f"run:{run.id}",
                                     action="run.finished", resource_type="analysis_run", resource_id=run.id,
                                     outcome=run.outcome or target.value.lower(),
                                     details={"state": target.value, "summary": truncate(transition.summary, 300)})
            run.state_json = ctx.state.model_dump(mode="json")
            for event_type, message, data in ctx.events:
                audit_service.emit_event(session, run.id, event_type, message, data)
            if transition.event_type:
                audit_service.emit_event(session, run.id, transition.event_type,
                                         transition.message or transition.summary,
                                         {**transition.data, "state": target.value})
            audit_service.emit_event(session, run.id, "state_changed", f"{from_state.value} -> {target.value}",
                                     {"from": from_state.value, "to": target.value})
            return target

        return with_write_retry(_do)

    @staticmethod
    def _final_report(session: Any, run: AnalysisRun, state: AgentState) -> FinalRepairReport:
        patch = session.scalar(select(ProposedPatch).where(ProposedPatch.run_id == run.id)
                               .order_by(ProposedPatch.attempt_number.desc()).limit(1))
        pr = session.scalar(select(PullRequest).where(PullRequest.run_id == run.id))
        limitations = list(state.notes[-5:])
        if state.revision_kind == "branch_head":
            limitations.insert(0, state.revision_note or "Analysis used a branch head, not a release commit.")
        status = run.diagnosis_status if run.diagnosis_status in {"provisional", "supported_by_tests"} \
            else "not_established"
        if run.outcome in {RunOutcome.ABSTAINED_INSUFFICIENT_EVIDENCE.value,
                           RunOutcome.ABSTAINED_EXTERNAL_CAUSE.value, RunOutcome.NO_REPOSITORY.value}:
            status = "not_established"
        simulated = []
        if run.llm_simulated:
            simulated.append("model")
        if state.validation.simulated:
            simulated.append("tests")
        if run.is_demo:
            simulated.append("GitHub publication")
        return FinalRepairReport(
            run_id=run.id, outcome=run.outcome or run.status, diagnosis_status=status,
            root_cause=run.root_cause_summary, confidence=run.confidence_score,
            files_changed=list(patch.files_changed_json) if patch else [],
            validation_outcome=state.validation.outcome, policy_status=patch.policy_status if patch else None,
            limitations=limitations, pull_request_url=pr.url if pr else None, simulated_components=simulated,
        )

    @staticmethod
    def _fail_step(step_id: int, error: str, started: float, *, retrying: bool) -> None:
        def _do(session: Any) -> None:
            step = session.get(AgentStep, step_id)
            if step is None:
                return
            step.status = StepStatus.FAILED.value
            step.error_message = truncate(redact(error), 2000)
            step.duration_ms = int((time.monotonic() - started) * 1000)
            step.completed_at = utcnow()
            run = session.get(AnalysisRun, step.run_id)
            if run is not None:
                run.retry_count += 1
                audit_service.emit_event(session, run.id, "retry_scheduled" if retrying else "step_failed",
                                         truncate(f"{step.step_type} failed: {redact(error)}", 900),
                                         {"retrying": retrying})

        with_write_retry(_do)

    # --- main loop -------------------------------------------------------------------------

    async def execute(self, run_id: int) -> RunState | None:
        deps: RunDependencies | None = None
        final: RunState | None = None
        try:
            deps = await asyncio.to_thread(self._build_deps, run_id)
            if deps is None:
                return None
            for _ in range(100):  # hard bound; includes repair-attempt loops
                loaded = await asyncio.to_thread(self._load, run_id)
                if loaded is None:
                    return None
                snapshot, state, cancel_requested = loaded
                current = RunState(snapshot.status)
                final = current
                if current in PARKED_STATES:
                    break
                if cancel_requested and is_cancellable(current):
                    final = await asyncio.to_thread(self._cancel, run_id, current, snapshot, state, deps)
                    continue
                handler = HANDLERS[current]
                step_id = await asyncio.to_thread(self._start_step, run_id, current)
                ctx = NodeContext(snapshot, state, deps)
                started = time.monotonic()
                try:
                    transition = await handler(ctx)
                except ExternalServiceError as exc:
                    if exc.retryable:
                        await asyncio.to_thread(self._fail_step, step_id, exc.message, started, retrying=True)
                        raise
                    transition = self._transition_for_error(exc)
                except Exception as exc:
                    transition = self._transition_for_error(exc)
                try:
                    final = await asyncio.to_thread(self._commit, run_id, step_id, current, ctx, transition,
                                                    started)
                except InvalidTransition as exc:
                    logger.error("invalid transition", extra={"run_id": run_id, "error": str(exc)})
                    failure = Transition(RunState.FAILED, str(exc), RunOutcome.ERROR, error=str(exc),
                                         event_type="run_failed", message="Internal workflow error")
                    final = await asyncio.to_thread(self._commit, run_id, step_id, current, ctx, failure, started)
            if final == RunState.NEEDS_HUMAN_REVIEW and deps.runtime.create_issue_on_human_review:
                await self._open_review_issue(run_id)
            return final
        finally:
            if deps is not None:
                await deps.aclose()
                if final is not None and final in PARKED_STATES:
                    cleanup_workspace(deps.workspace_root, run_id)

    async def _open_review_issue(self, run_id: int) -> None:
        """Optional: track runs that need human judgement as GitHub issues (deduplicated)."""
        from app.services import publication_service

        try:
            await publication_service.create_github_issue(self.factory, run_id, f"agent:run:{run_id}")
        except Exception as exc:  # best effort: the run outcome is already recorded
            message = str(getattr(exc, "message", exc))[:300]
            await asyncio.to_thread(with_write_retry, lambda s: audit_service.emit_event(
                s, run_id, "github_issue_failed", f"Could not create a GitHub issue: {message}"))

    def _build_deps(self, run_id: int) -> RunDependencies | None:
        with session_scope() as session:
            run = session.get(AnalysisRun, run_id)
            return self.factory.for_run(session, run) if run is not None else None

    def _cancel(self, run_id: int, current: RunState, snapshot: RunSnapshot, state: AgentState,
                deps: RunDependencies) -> RunState:
        step_id = self._start_step(run_id, current)
        ctx = NodeContext(snapshot, state, deps)
        return self._commit(run_id, step_id, current, ctx,
                            Transition(RunState.CANCELLED, "Cancelled by user", RunOutcome.CANCELLED,
                                       event_type="run_cancelled", message="Run cancelled"), time.monotonic())

    @staticmethod
    def _transition_for_error(exc: BaseException) -> Transition:
        if isinstance(exc, CancelledByUser):
            return Transition(RunState.CANCELLED, "Cancelled", RunOutcome.CANCELLED, event_type="run_cancelled",
                              message="Run cancelled")
        if isinstance(exc, PolicyViolation):
            return Transition(RunState.NEEDS_HUMAN_REVIEW, f"Policy: {exc.message}", RunOutcome.POLICY_BLOCKED,
                              error=exc.message, event_type="needs_human_review", message=exc.message)
        if isinstance(exc, NotConfiguredError):
            return Transition(RunState.FAILED, exc.message, RunOutcome.ERROR, error=exc.message,
                              event_type="run_failed", message=exc.message)
        if isinstance(exc, (ExternalAuthError, ExternalNotFound)):
            message = f"Access to {exc.service} failed: {exc.message}"
            return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.ERROR, error=message,
                              event_type="needs_human_review", message=message)
        if isinstance(exc, LLMOutputError):
            message = f"The model did not return valid structured output after bounded retries ({exc.message[:300]})"
            return Transition(RunState.NEEDS_HUMAN_REVIEW, message, RunOutcome.ERROR, error=message,
                              event_type="needs_human_review", message=message)
        if isinstance(exc, ExternalServiceError):
            return Transition(RunState.FAILED, exc.message, RunOutcome.ERROR, error=exc.message,
                              event_type="run_failed", message=exc.message)
        logger.exception("unexpected workflow error", exc_info=exc)
        message = f"Unexpected error: {type(exc).__name__}"
        return Transition(RunState.FAILED, message, RunOutcome.ERROR, error=f"{message}: {str(exc)[:500]}",
                          event_type="run_failed", message=message)


def mark_run_failed(run_id: int, error: str) -> None:
    """Called when a workflow job exhausted its retries."""

    def _do(session: Any) -> None:
        run = session.get(AnalysisRun, run_id)
        if run is None or RunState(run.status) in TERMINAL_STATES:
            return
        try:
            validate_transition(run.status, RunState.FAILED)
        except InvalidTransition:
            return
        run.status = RunState.FAILED.value
        run.outcome = RunOutcome.ERROR.value
        run.error_message = truncate(redact(error), 2000)
        run.completed_at = utcnow()
        audit_service.emit_event(session, run.id, "run_failed", truncate(f"Run failed: {redact(error)}", 900))
        audit_service.record(session, actor_type="system", actor_id="worker", action="run.finished",
                             resource_type="analysis_run", resource_id=run.id, outcome="error",
                             details={"error": truncate(error, 300)})

    with_write_retry(_do)
