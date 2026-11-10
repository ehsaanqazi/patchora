"""Evaluation metrics computed from measured per-case results (never estimated)."""

from __future__ import annotations

import math
import statistics
from typing import Any


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(p / 100 * len(ordered))  # nearest-rank method
    return ordered[min(len(ordered), max(1, rank)) - 1]


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [r for r in results if not r.get("skipped")]
    with_repo = [r for r in evaluated if r["expect_repository"]]
    repo_ok = sum(1 for r in evaluated if r["repository_resolved"] == r["expect_repository"])

    precision_values = [r["retrieval_precision"] for r in evaluated if r["retrieval_precision"] is not None]
    recall_values = [r["retrieval_recall"] for r in evaluated if r["retrieval_recall"] is not None]

    rc_cases = [r for r in evaluated if r["root_cause_expected"]]
    rc_good = sum(1 for r in rc_cases if r["root_cause_cites_relevant_file"])

    patch_cases = [r for r in evaluated if r["patch_generated"]]
    applied = [r for r in patch_cases if r["patch_applied"]]
    tested = [r for r in evaluated if r["final_validation"] in {"passed", "failed", "timed_out"}]
    passed = [r for r in tested if r["final_validation"] == "passed"]

    abstain_expected = [r for r in evaluated if r["expect_abstain"]]
    abstain_correct = sum(1 for r in abstain_expected if r["abstained"])
    false_abstentions = sum(1 for r in evaluated if not r["expect_abstain"] and r["abstained"])

    pr_expected = [r for r in evaluated if r["expect_pull_request"]]
    pr_ok = sum(1 for r in pr_expected if r["pull_request_created"])
    unexpected_prs = sum(1 for r in evaluated if not r["expect_pull_request"] and r["pull_request_created"])

    durations = [r["duration_ms"] for r in evaluated if r["duration_ms"] is not None]
    outcome_ok = sum(1 for r in evaluated if r["outcome_correct"])
    accepted_with_regression = sum(1 for r in evaluated if r["final_regression"] and r["outcome"] in
                                   {"awaiting_approval", "pull_request_created"})
    return {
        "cases_total": len(results),
        "cases_evaluated": len(evaluated),
        "cases_skipped": len(results) - len(evaluated),
        "outcome_accuracy": _ratio(outcome_ok, len(evaluated)),
        "repository_resolution_accuracy": _ratio(repo_ok, len(evaluated)),
        "repository_cases": len(with_repo),
        "relevant_file_retrieval_precision": round(statistics.mean(precision_values), 3) if precision_values else None,
        "relevant_file_retrieval_recall": round(statistics.mean(recall_values), 3) if recall_values else None,
        "root_cause_cites_relevant_code_rate": _ratio(rc_good, len(rc_cases)),
        "patch_application_success_rate": _ratio(len(applied), len(patch_cases)),
        "test_pass_rate": _ratio(len(passed), len(tested)),
        "regressions_detected": sum(r["regressions_detected"] for r in evaluated),
        "accepted_patches_with_known_regression": accepted_with_regression,
        "correct_abstention_rate": _ratio(abstain_correct, len(abstain_expected)),
        "false_abstentions": false_abstentions,
        "pr_creation_success_rate": _ratio(pr_ok, len(pr_expected)),
        "unexpected_pull_requests": unexpected_prs,
        "duplicate_prevention_passed": all(r["outcome_correct"] for r in evaluated if r["category"] == "idempotency"),
        "runtime_ms_median": int(statistics.median(durations)) if durations else None,
        "runtime_ms_p90": _percentile(durations, 90),
        "runtime_ms_p95": _percentile(durations, 95),
        "prompt_tokens_total": sum(r["prompt_tokens"] for r in evaluated),
        "completion_tokens_total": sum(r["completion_tokens"] for r in evaluated),
        "estimated_cost_usd": (round(sum(r["estimated_cost"] for r in evaluated if r["estimated_cost"]), 6)
                               if any(r["estimated_cost"] for r in evaluated) else None),
        "average_repair_attempts": round(statistics.mean(r["repair_attempts"] for r in evaluated), 3)
        if evaluated else None,
    }
