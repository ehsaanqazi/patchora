"""Deterministic gates between agent steps.

These checks decide what happens next; the model's own confidence or claims of safety are
inputs to be checked, never decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import AutonomyMode
from app.core.enums import ValidationOutcome
from app.schemas.agent_outputs import Diagnosis, PatchPlan
from app.security.input_sanitization import normalize_repo_path, path_matches
from app.services.patch_service import is_test_path

MIN_CONFIDENCE_TO_PATCH = 0.5


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def sanitize_diagnosis(diagnosis: Diagnosis, evidence_ids: set[str]) -> tuple[Diagnosis, list[str]]:
    """Drop citations to evidence that does not exist; uncited hypotheses lose confidence."""
    warnings: list[str] = []
    hypotheses = []
    for hypothesis in diagnosis.hypotheses:
        valid = [e for e in hypothesis.evidence_ids if e in evidence_ids]
        invalid = set(hypothesis.evidence_ids) - set(valid)
        if invalid:
            warnings.append(f"Removed citations to unknown evidence: {sorted(invalid)}")
        confidence = hypothesis.confidence if valid else min(hypothesis.confidence, 0.2)
        hypotheses.append(hypothesis.model_copy(update={"evidence_ids": valid, "confidence": confidence}))
    evidence = [e for e in diagnosis.evidence if e.evidence_id in evidence_ids]
    cleaned = diagnosis.model_copy(update={"hypotheses": sorted(hypotheses, key=lambda h: h.confidence,
                                                                 reverse=True),
                                           "evidence": evidence})
    return cleaned, warnings


def evidence_gate(diagnosis: Diagnosis, investigation: dict[str, Any]) -> GateResult:
    reasons: list[str] = []
    top = diagnosis.hypotheses[0]
    kinds = {e["kind"] for e in investigation.get("evidence", [])}
    if diagnosis.error_classification != "code_defect":
        reasons.append(f"The failure is classified as '{diagnosis.error_classification}', not a code defect "
                       "in this repository.")
    if not diagnosis.evidence_sufficient:
        reasons.append("The diagnosis reports that the available evidence is insufficient.")
    if not top.evidence_ids:
        reasons.append("The leading hypothesis does not cite any collected evidence.")
    if "stack_frame" not in kinds:
        reasons.append("No application stack frame could be mapped to repository code.")
    if top.confidence < MIN_CONFIDENCE_TO_PATCH:
        reasons.append(f"Leading hypothesis confidence {top.confidence:.2f} is below "
                       f"{MIN_CONFIDENCE_TO_PATCH:.2f}.")
    return GateResult(not reasons, reasons)


def plan_gate(plan: PatchPlan, tree_paths: set[str], protected: list[str], max_files: int) -> GateResult:
    reasons: list[str] = []
    if not plan.should_patch:
        return GateResult(False, [plan.abstain_reason or "The plan declined to patch."])
    targets = [f.path for f in plan.files_to_modify] + [t.path for t in plan.tests_to_add_or_update]
    if not plan.files_to_modify:
        reasons.append("The plan does not identify any file to modify.")
    if len(set(targets)) > max_files:
        reasons.append(f"The plan touches {len(set(targets))} files (limit {max_files}).")
    for raw in targets:
        try:
            path = normalize_repo_path(raw)
        except Exception as exc:
            reasons.append(f"Invalid path in plan: {raw!r} ({exc})")
            continue
        if path_matches(path, protected):
            reasons.append(f"The plan modifies protected path {path}.")
        if path not in tree_paths and not is_test_path(path):
            reasons.append(f"{path} does not exist at the pinned revision (only new test files may be created).")
    return GateResult(not reasons, reasons)


def needs_approval(mode: AutonomyMode, *, risk: str, validation: str, reproduced: bool,
                   overlapping_changes: bool, auto_pr_enabled: bool) -> tuple[bool, list[str]]:
    """Whether publication requires a human. Mode B always does; Mode C only for clean cases."""
    reasons: list[str] = []
    if mode == AutonomyMode.PREPARE_PATCH:
        reasons.append("Autonomy mode B requires explicit approval before publication.")
    if mode == AutonomyMode.AUTO_PR and not auto_pr_enabled:
        reasons.append("AUTO_CREATE_PULL_REQUESTS is disabled.")
    if risk != "low":
        reasons.append(f"Patch risk is {risk}.")
    if validation != ValidationOutcome.PASSED:
        reasons.append(f"Validation outcome is '{validation}', not 'passed'.")
    if overlapping_changes:
        reasons.append("Files in the patch changed on the target branch since the analyzed revision.")
    if not reproduced:
        # Mode C only publishes unattended when a test failed before the fix and passes after it.
        reasons.append("No test reproduced the failure before the fix; the diagnosis is provisional.")
    return bool(reasons), reasons


PUBLISHABLE_OUTCOMES = {ValidationOutcome.PASSED, ValidationOutcome.NOT_AVAILABLE}


def publication_blockers(*, validation: str, static_ok: bool, has_human_approval: bool,
                         repository_enabled: bool, policy_blocked: bool, sandbox_simulated: bool,
                         is_demo: bool) -> list[str]:
    """Hard requirements that no approval can override."""
    blockers: list[str] = []
    if not repository_enabled:
        blockers.append("The repository is no longer on the allowlist.")
    if policy_blocked:
        blockers.append("The patch violates a security policy.")
    if validation not in PUBLISHABLE_OUTCOMES:
        blockers.append(f"Validation outcome '{validation}' does not permit publication "
                        "(tests must run in the configured sandbox).")
    if validation == ValidationOutcome.NOT_AVAILABLE and not (static_ok and has_human_approval):
        blockers.append("The repository has no tests: publication requires passing static checks and "
                        "explicit human approval.")
    if sandbox_simulated and not is_demo:
        blockers.append("Simulated validation results cannot be used for real publication.")
    return blockers
