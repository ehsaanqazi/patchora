"""Durable agent working state, persisted in ``analysis_runs.state_json`` after every step.

It contains only bounded, non-secret data needed to resume the workflow: normalized issue
and event data, resolved repository/revision, evidence excerpts, the validated diagnosis
and plan, feedback from previous attempts and publication progress.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RepositoryRef(BaseModel):
    id: int
    full_name: str
    default_branch: str
    base_branch: str
    provider: str


class PublishState(BaseModel):
    branch: str | None = None
    head_sha: str | None = None
    base_branch_head: str | None = None
    overlapping_changes: list[str] = Field(default_factory=list)
    pr_number: int | None = None
    pr_url: str | None = None


class ValidationState(BaseModel):
    outcome: str | None = None
    reproduced: bool = False
    regression: bool = False
    summary: str | None = None
    backend: str | None = None
    simulated: bool = False
    test_targets: list[str] = Field(default_factory=list)


class AgentState(BaseModel):
    model_config = ConfigDict(extra="allow")

    issue: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    event_error: str | None = None
    release: str | None = None
    repository: RepositoryRef | None = None
    revision: str | None = None
    revision_kind: str | None = None  # release_commit | branch_head
    revision_note: str | None = None
    investigation: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    patch_feedback: list[str] = Field(default_factory=list)
    current_patch_id: int | None = None
    validation: ValidationState = Field(default_factory=ValidationState)
    validation_history: list[dict[str, Any]] = Field(default_factory=list)  # one entry per repair attempt
    publish: PublishState = Field(default_factory=PublishState)
    approval_id: int | None = None
    notes: list[str] = Field(default_factory=list)
    use_real_llm: bool = False
    demo_scenario: str | None = None

    def add_note(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note[:500])
        del self.notes[:-30]
