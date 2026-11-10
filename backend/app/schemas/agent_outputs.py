"""Pydantic schemas for every structured model output.

Model output is untrusted: it is validated against these schemas, then checked again by
deterministic code (evidence ids must exist, paths must exist in the repository revision,
limits are enforced by policy) before any action is taken.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


ErrorClassification = Literal[
    "code_defect", "external_dependency", "infrastructure", "configuration", "data_quality", "unknown"
]


class IssueSummary(_Strict):
    summary: str = Field(max_length=1000)
    error_type: str = Field(default="", max_length=200)
    affected_component: str = Field(default="", max_length=300)


class EvidenceReference(_Strict):
    evidence_id: str = Field(max_length=20)
    path: str = Field(default="", max_length=400)
    start_line: int | None = None
    end_line: int | None = None
    relevance: str = Field(default="", max_length=500)


class RootCauseHypothesis(_Strict):
    statement: str = Field(max_length=1500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)


class RelevantFileCandidate(_Strict):
    path: str = Field(max_length=400)
    reason: str = Field(default="", max_length=500)


class Diagnosis(_Strict):
    issue_summary: IssueSummary
    error_classification: ErrorClassification = "unknown"
    hypotheses: list[RootCauseHypothesis] = Field(min_length=1, max_length=5)
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=20)
    evidence_sufficient: bool
    relevant_files: list[RelevantFileCandidate] = Field(default_factory=list, max_length=15)
    missing_evidence: list[str] = Field(default_factory=list, max_length=10)
    additional_files_requested: list[str] = Field(default_factory=list, max_length=5)
    uncertainty: str = Field(default="", max_length=1500)

    @field_validator("hypotheses")
    @classmethod
    def _sorted(cls, value: list[RootCauseHypothesis]) -> list[RootCauseHypothesis]:
        return sorted(value, key=lambda h: h.confidence, reverse=True)


class FileChangePlan(_Strict):
    path: str = Field(max_length=400)
    change_description: str = Field(max_length=1000)


class TestChangePlan(_Strict):
    __test__ = False
    path: str = Field(max_length=400)
    description: str = Field(default="", max_length=1000)


class TestRecommendation(_Strict):
    __test__ = False
    test_files: list[str] = Field(default_factory=list, max_length=10)
    rationale: str = Field(default="", max_length=1000)


class PatchPlan(_Strict):
    should_patch: bool
    abstain_reason: str = Field(default="", max_length=1000)
    root_cause: str = Field(default="", max_length=1500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    files_to_modify: list[FileChangePlan] = Field(default_factory=list, max_length=20)
    tests_to_add_or_update: list[TestChangePlan] = Field(default_factory=list, max_length=10)
    regression_risks: list[str] = Field(default_factory=list, max_length=10)
    test_recommendations: TestRecommendation = Field(default_factory=TestRecommendation)
    risk_level: Literal["low", "medium", "high"] = "medium"


class PatchEdit(_Strict):
    path: str = Field(max_length=400)
    action: Literal["modify", "create"] = "modify"
    # For "modify": exact text that must occur exactly once in the current file.
    search: str = Field(default="", max_length=20_000)
    replace: str = Field(max_length=40_000)

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


class PatchProposal(_Strict):
    """Patch metadata plus edits. Converted deterministically into a unified diff."""

    summary: str = Field(max_length=1500)
    edits: list[PatchEdit] = Field(min_length=1, max_length=20)
    tests_added: list[str] = Field(default_factory=list, max_length=10)


class ValidationSummary(_Strict):
    """Model analysis of a failed validation, used to inform the next repair attempt."""

    failure_summary: str = Field(max_length=1500)
    patch_caused_failure: bool
    retry_recommended: bool
    suggested_adjustment: str = Field(default="", max_length=1500)


class FinalRepairReport(_Strict):
    """Deterministically assembled from persisted run data (not model-generated)."""

    run_id: int
    outcome: str
    diagnosis_status: Literal["provisional", "supported_by_tests", "not_established"]
    root_cause: str | None = None
    confidence: float | None = None
    files_changed: list[str] = Field(default_factory=list)
    validation_outcome: str | None = None
    policy_status: str | None = None
    limitations: list[str] = Field(default_factory=list)
    pull_request_url: str | None = None
    simulated_components: list[str] = Field(default_factory=list)
