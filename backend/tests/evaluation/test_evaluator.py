"""The evaluation harness runs cases through the real engine and reports measured metrics."""

from __future__ import annotations

import json

from app.evaluation.dataset import build_cases
from app.evaluation.evaluator import evaluate
from app.evaluation.metrics import compute_metrics


def test_dataset_covers_required_categories():
    cases = {c.key: c for c in build_cases()}
    assert len(cases) >= 12
    for key in ("missing-null-check", "boundary-condition", "incorrect-exception-handling", "missing-regression-test",
                "external-root-cause", "ambiguous-stack-trace", "prompt-injection", "no-relevant-tests",
                "fix-introduces-regression", "malformed-model-response", "repository-access-failure", "test-timeout"):
        assert key in cases
    assert all(c.expected_outcomes for c in cases.values())


async def test_scripted_evaluation_subset(settings, tmp_path):
    output = tmp_path / "reports"
    report = await evaluate("scripted", "local",
                            ["external-root-cause", "prompt-injection", "malformed-model-response",
                             "boundary-condition"], output)
    metrics = report["metrics"]
    assert metrics["cases_evaluated"] == 4 and metrics["outcome_accuracy"] == 1.0
    assert metrics["prompt_tokens_total"] == 0  # scripted responses consume no tokens; nothing is estimated
    assert metrics["estimated_cost_usd"] is None
    boundary = next(r for r in report["results"] if r["key"] == "boundary-condition")
    assert boundary["validation_history"][0]["reproduced"] is True  # real pytest run on the fixture
    files = sorted(p.name for p in output.iterdir())
    assert len(files) == 2 and files[0].endswith(".json")
    assert json.loads((output / files[0]).read_text())["dataset_version"]


def test_metrics_never_invent_values():
    metrics = compute_metrics([])
    assert metrics["outcome_accuracy"] is None and metrics["runtime_ms_median"] is None
