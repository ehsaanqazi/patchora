"""Explicit, validated workflow transitions.

Only the transitions listed here are possible. Terminal states have no outgoing edges; a
"retry" of a finished run creates a new run linked through ``retry_of_run_id``.
"""

from __future__ import annotations

from app.core.enums import TERMINAL_STATES, RunState

S = RunState
_ABORT = {S.FAILED, S.CANCELLED}

TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    S.RECEIVED: frozenset({S.VALIDATING} | _ABORT),
    S.VALIDATING: frozenset({S.DEDUPLICATING} | _ABORT),
    S.DEDUPLICATING: frozenset({S.FETCHING_SENTRY_DATA, S.COMPLETED} | _ABORT),
    S.FETCHING_SENTRY_DATA: frozenset({S.MAPPING_REPOSITORY} | _ABORT),
    S.MAPPING_REPOSITORY: frozenset({S.FETCHING_SOURCE, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    S.FETCHING_SOURCE: frozenset({S.INVESTIGATING, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    S.INVESTIGATING: frozenset({S.DIAGNOSIS_READY, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    S.DIAGNOSIS_READY: frozenset({S.PLANNING_PATCH, S.NEEDS_HUMAN_REVIEW, S.COMPLETED} | _ABORT),
    S.PLANNING_PATCH: frozenset({S.GENERATING_PATCH, S.NEEDS_HUMAN_REVIEW, S.COMPLETED} | _ABORT),
    S.GENERATING_PATCH: frozenset({S.REVIEWING_PATCH, S.GENERATING_PATCH, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    S.REVIEWING_PATCH: frozenset(
        {S.APPLYING_PATCH, S.GENERATING_PATCH, S.COMPLETED, S.NEEDS_HUMAN_REVIEW} | _ABORT
    ),
    S.APPLYING_PATCH: frozenset({S.RUNNING_TESTS, S.GENERATING_PATCH, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    S.RUNNING_TESTS: frozenset({S.EVALUATING_RESULTS, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    S.EVALUATING_RESULTS: frozenset(
        {S.WAITING_FOR_APPROVAL, S.READY_TO_PUBLISH, S.GENERATING_PATCH, S.NEEDS_HUMAN_REVIEW,
         S.COMPLETED} | _ABORT
    ),
    S.WAITING_FOR_APPROVAL: frozenset({S.READY_TO_PUBLISH, S.CANCELLED, S.FAILED}),
    S.READY_TO_PUBLISH: frozenset({S.PUBLISHING_BRANCH, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    S.PUBLISHING_BRANCH: frozenset({S.CREATING_PULL_REQUEST, S.NEEDS_HUMAN_REVIEW} | _ABORT),
    # No cancellation once the branch exists: the PR step finishes or fails visibly.
    S.CREATING_PULL_REQUEST: frozenset({S.COMPLETED, S.NEEDS_HUMAN_REVIEW, S.FAILED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
    S.NEEDS_HUMAN_REVIEW: frozenset(),
}

assert set(TRANSITIONS) == set(RunState), "every state must have a transition entry"
assert all(not TRANSITIONS[s] for s in TERMINAL_STATES)


class InvalidTransition(Exception):
    pass


def can_transition(current: RunState, target: RunState) -> bool:
    return target in TRANSITIONS[current]


def validate_transition(current: RunState | str, target: RunState | str) -> RunState:
    cur, tgt = RunState(current), RunState(target)
    if not can_transition(cur, tgt):
        raise InvalidTransition(f"Transition {cur.value} -> {tgt.value} is not allowed")
    return tgt


def is_cancellable(state: RunState | str) -> bool:
    return RunState.CANCELLED in TRANSITIONS[RunState(state)]
