"""Shared enumerations stored in the database and exposed through the API."""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    DEDUPLICATING = "DEDUPLICATING"
    FETCHING_SENTRY_DATA = "FETCHING_SENTRY_DATA"
    MAPPING_REPOSITORY = "MAPPING_REPOSITORY"
    FETCHING_SOURCE = "FETCHING_SOURCE"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSIS_READY = "DIAGNOSIS_READY"
    PLANNING_PATCH = "PLANNING_PATCH"
    GENERATING_PATCH = "GENERATING_PATCH"
    REVIEWING_PATCH = "REVIEWING_PATCH"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPLYING_PATCH = "APPLYING_PATCH"
    RUNNING_TESTS = "RUNNING_TESTS"
    EVALUATING_RESULTS = "EVALUATING_RESULTS"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHING_BRANCH = "PUBLISHING_BRANCH"
    CREATING_PULL_REQUEST = "CREATING_PULL_REQUEST"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


TERMINAL_STATES = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.NEEDS_HUMAN_REVIEW}
)
# States in which the run is parked and no worker is processing it.
PARKED_STATES = TERMINAL_STATES | {RunState.WAITING_FOR_APPROVAL}


class RunOutcome(StrEnum):
    PULL_REQUEST_CREATED = "pull_request_created"
    AWAITING_APPROVAL = "awaiting_approval"
    ANALYSIS_ONLY = "analysis_only"
    PATCH_VALIDATED = "patch_validated"
    ABSTAINED_INSUFFICIENT_EVIDENCE = "abstained_insufficient_evidence"
    ABSTAINED_EXTERNAL_CAUSE = "abstained_external_cause"
    NO_REPOSITORY = "no_repository"
    POLICY_BLOCKED = "policy_blocked"
    TESTS_FAILED = "tests_failed"
    PATCH_INVALID = "patch_invalid"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"
    ERROR = "error"


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ValidationOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    NOT_AVAILABLE = "not_available"  # repository has no tests
    NOT_RUN = "not_run"  # execution disabled or not permitted
    SETUP_FAILED = "setup_failed"
    SECURITY_BLOCKED = "security_blocked"
    PENDING = "pending"


class PolicyStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalType(StrEnum):
    PUBLISH_PULL_REQUEST = "publish_pull_request"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    RUN_WORKFLOW = "run_workflow"
    PROCESS_WEBHOOK = "process_webhook"


class UserRole(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class DeliveryStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"
    DUPLICATE = "duplicate"
