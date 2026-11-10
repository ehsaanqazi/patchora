from __future__ import annotations

import pytest

from app.agent import policies
from app.agent.transitions import TRANSITIONS, InvalidTransition, can_transition, validate_transition
from app.config import AutonomyMode
from app.core.enums import TERMINAL_STATES, RunState
from app.schemas.agent_outputs import Diagnosis, PatchPlan


def test_terminal_states_have_no_exits():
    for state in TERMINAL_STATES:
        assert not TRANSITIONS[state]


def test_invalid_transitions_rejected():
    assert can_transition(RunState.RECEIVED, RunState.VALIDATING)
    with pytest.raises(InvalidTransition):
        validate_transition(RunState.RECEIVED, RunState.CREATING_PULL_REQUEST)
    with pytest.raises(InvalidTransition):
        validate_transition(RunState.COMPLETED, RunState.RECEIVED)
    with pytest.raises(InvalidTransition):  # approval cannot be skipped from analysis
        validate_transition(RunState.INVESTIGATING, RunState.PUBLISHING_BRANCH)


def test_every_state_can_reach_a_terminal_state():
    for start in RunState:
        seen, stack = set(), [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(TRANSITIONS[current])
        assert seen & TERMINAL_STATES, start


def test_publication_only_reachable_through_ready_to_publish():
    sources = {s for s, targets in TRANSITIONS.items() if RunState.PUBLISHING_BRANCH in targets}
    assert sources == {RunState.READY_TO_PUBLISH}
    sources = {s for s, targets in TRANSITIONS.items() if RunState.READY_TO_PUBLISH in targets}
    assert sources == {RunState.EVALUATING_RESULTS, RunState.WAITING_FOR_APPROVAL}


def _diagnosis(**kw) -> Diagnosis:
    base = {"issue_summary": {"summary": "s"}, "error_classification": "code_defect",
            "hypotheses": [{"statement": "h", "evidence_ids": ["E1"], "confidence": 0.8}],
            "evidence_sufficient": True}
    base.update(kw)
    return Diagnosis.model_validate(base)


INV = {"evidence": [{"id": "E1", "kind": "stack_frame"}]}


def test_evidence_gate():
    assert policies.evidence_gate(_diagnosis(), INV).ok
    assert not policies.evidence_gate(_diagnosis(error_classification="infrastructure"), INV).ok
    assert not policies.evidence_gate(_diagnosis(evidence_sufficient=False), INV).ok
    assert not policies.evidence_gate(_diagnosis(), {"evidence": [{"id": "E1", "kind": "dependency"}]}).ok
    low = _diagnosis(hypotheses=[{"statement": "h", "evidence_ids": ["E1"], "confidence": 0.3}])
    assert not policies.evidence_gate(low, INV).ok


def test_high_model_confidence_without_evidence_is_downgraded():
    diagnosis = _diagnosis(hypotheses=[{"statement": "h", "evidence_ids": ["E9"], "confidence": 0.99}])
    cleaned, warnings = policies.sanitize_diagnosis(diagnosis, {"E1"})
    assert cleaned.hypotheses[0].evidence_ids == []
    assert cleaned.hypotheses[0].confidence <= 0.2
    assert warnings
    assert not policies.evidence_gate(cleaned, INV).ok


def test_plan_gate():
    tree = {"src/a.py", "tests/test_a.py"}
    plan = PatchPlan(should_patch=True, files_to_modify=[{"path": "src/a.py", "change_description": "x"}])
    assert policies.plan_gate(plan, tree, [".github/**"], 5).ok
    bad = PatchPlan(should_patch=True, files_to_modify=[{"path": ".github/workflows/ci.yml", "change_description": "x"}])
    result = policies.plan_gate(bad, tree | {".github/workflows/ci.yml"}, [".github/**"], 5)
    assert not result.ok and any("protected" in r for r in result.reasons)
    ghost = PatchPlan(should_patch=True, files_to_modify=[{"path": "src/ghost.py", "change_description": "x"}])
    assert not policies.plan_gate(ghost, tree, [], 5).ok
    traversal = PatchPlan(should_patch=True, files_to_modify=[{"path": "../etc/passwd", "change_description": "x"}])
    assert not policies.plan_gate(traversal, tree, [], 5).ok
    abstain = PatchPlan(should_patch=False, abstain_reason="not safe")
    assert policies.plan_gate(abstain, tree, [], 5).reasons == ["not safe"]


def test_approval_requirements():
    required, _ = policies.needs_approval(AutonomyMode.PREPARE_PATCH, risk="low", validation="passed",
                                          reproduced=True, overlapping_changes=False, auto_pr_enabled=True)
    assert required
    required, _ = policies.needs_approval(AutonomyMode.AUTO_PR, risk="low", validation="passed",
                                          reproduced=True, overlapping_changes=False, auto_pr_enabled=True)
    assert not required
    for kwargs in ({"risk": "medium"}, {"reproduced": False}, {"auto_pr_enabled": False},
                   {"validation": "not_available"}):
        args = {"risk": "low", "validation": "passed", "reproduced": True, "overlapping_changes": False,
                "auto_pr_enabled": True} | kwargs
        assert policies.needs_approval(AutonomyMode.AUTO_PR, **args)[0], kwargs


def test_publication_blockers_cannot_be_overridden_by_approval():
    base = dict(validation="passed", static_ok=True, has_human_approval=True, repository_enabled=True,
                policy_blocked=False, sandbox_simulated=False, is_demo=False)
    assert policies.publication_blockers(**base) == []
    assert policies.publication_blockers(**base | {"validation": "not_run"})
    assert policies.publication_blockers(**base | {"validation": "failed"})
    assert policies.publication_blockers(**base | {"repository_enabled": False})
    assert policies.publication_blockers(**base | {"policy_blocked": True})
    assert policies.publication_blockers(**base | {"sandbox_simulated": True})
    assert policies.publication_blockers(**base | {"validation": "not_available", "has_human_approval": False})
    assert policies.publication_blockers(**base | {"validation": "not_available"}) == []
