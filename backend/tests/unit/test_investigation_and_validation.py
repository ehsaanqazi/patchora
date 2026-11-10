from __future__ import annotations

from pathlib import Path

from app.core.enums import ValidationOutcome
from app.sandbox.result import ExecutionResult, parse_test_counts
from app.schemas.issues import StackFrame
from app.services.repository_service import (
    PathResolver,
    application_frames,
    numbered_excerpt,
    related_test_candidates,
)
from app.services.validation_service import (
    PhaseResult,
    TestPlan,
    classify,
    detect_test_plan,
    evaluate,
    validate_test_targets,
)

TREE = {"shop/pricing.py", "shop/users.py", "tests/test_pricing.py", "lib/utils.py", "app/utils.py",
        "shop/__init__.py"}


def test_path_resolution_strategies():
    resolver = PathResolver(TREE, ["/srv/app/"], ["shop/"])
    assert resolver.resolve(StackFrame(abs_path="/srv/app/shop/pricing.py")) == ("shop/pricing.py", "exact")
    assert resolver.resolve(StackFrame(filename="pricing.py")) == ("shop/pricing.py", "path_prefix")
    assert resolver.resolve(StackFrame(module="shop.users", filename="<x>"))[0] == "shop/users.py"
    assert resolver.resolve(StackFrame(abs_path="/deploy/build/shop/pricing.py"))[1] == "suffix"
    # Two candidate files with the same name and no disambiguating prefix: never guess.
    assert resolver.resolve(StackFrame(filename="utils.py")) == (None, None)


def test_application_frames_prefers_in_app_and_innermost_first():
    frames = [StackFrame(filename="a.py", in_app=True), StackFrame(filename="site-packages/x.py", in_app=False),
              StackFrame(filename="b.py", in_app=True)]
    assert [f.filename for _, f in application_frames(frames)] == ["b.py", "a.py"]
    unmarked = [StackFrame(abs_path="/usr/lib/python3.12/x.py"), StackFrame(abs_path="/srv/app/m.py")]
    assert [f.abs_path for _, f in application_frames(unmarked)] == ["/srv/app/m.py"]


def test_numbered_excerpt_and_related_tests():
    text = "\n".join(f"line {i}" for i in range(1, 101))
    excerpt, start, end = numbered_excerpt(text, 50, radius=2)
    assert (start, end) == (48, 52) and "50 | line 50" in excerpt
    assert related_test_candidates("shop/pricing.py", TREE) == ["tests/test_pricing.py"]


def _write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_detect_python_plan_and_target_validation(tmp_path):
    _write(tmp_path, {"requirements.txt": "", "pkg/a.py": "x=1\n", "tests/test_a.py": "def test(): pass\n"})
    plan = detect_test_plan(tmp_path, repo_commands=None,
                            test_targets=["tests/test_a.py", "../evil.py", "-k", "pkg/a.py", "tests/missing.py"],
                            changed_files=["pkg/a.py"], python_image="py", node_image="node",
                            allow_dependency_install=True)
    assert plan.ecosystem == "python" and plan.has_tests
    assert plan.targeted == [["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no",
                              "tests/test_a.py"]]
    assert plan.setup[0][:3] == ["pip", "install", "--only-binary=:all:"]
    assert ["python", "-m", "py_compile", "pkg/a.py"] in plan.static
    assert validate_test_targets(tmp_path, ["tests/test_a.py::test_x"], ["tests/test_a.py"]) == ["tests/test_a.py"]


def test_detect_node_and_unknown(tmp_path):
    node = tmp_path / "node"
    _write(node, {"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
                  "index.js": "1"})
    plan = detect_test_plan(node, repo_commands=None, test_targets=[], changed_files=["index.js"],
                            python_image="py", node_image="node", allow_dependency_install=False)
    assert plan.ecosystem == "node" and not plan.has_tests and plan.setup == []
    assert plan.static == [["node", "--check", "index.js"]]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect_test_plan(empty, repo_commands=None, test_targets=[], changed_files=[], python_image="p",
                            node_image="n", allow_dependency_install=True).ecosystem == "unknown"
    bare = tmp_path / "bare"
    _write(bare, {"lib/names.py": "x = 1\n"})
    plan = detect_test_plan(bare, repo_commands=None, test_targets=[], changed_files=["lib/names.py"],
                            python_image="p", node_image="n", allow_dependency_install=True)
    assert not plan.has_tests and plan.static == [["python", "-m", "py_compile", "lib/names.py"]]


def test_configured_commands_are_validated(tmp_path):
    plan = detect_test_plan(tmp_path, repo_commands=[{"argv": ["pytest", "-q"], "phase": "test"},
                                                      {"argv": ["npm", "ci"], "phase": "setup"}],
                            test_targets=[], changed_files=[], python_image="p", node_image="n",
                            allow_dependency_install=True)
    assert plan.full == [["pytest", "-q"]] and plan.setup == [["npm", "ci"]] and plan.image == "n"


def _phase(phase: str, code: int | None, timed_out: bool = False, error: str | None = None) -> PhaseResult:
    result = ExecutionResult(exit_code=code, stdout="", stderr="", duration_ms=1, timed_out=timed_out, error=error)
    return PhaseResult(phase, ["python", "-m", "pytest"], result, classify(result, is_pytest=True))


def test_validation_verdicts():
    plan = TestPlan(ecosystem="python", image=None, has_tests=True)
    reproduced = evaluate([_phase("baseline", 1), _phase("patched", 0)], plan)
    assert reproduced.outcome == ValidationOutcome.PASSED and reproduced.reproduced
    regression = evaluate([_phase("baseline", 0), _phase("patched", 1)], plan)
    assert regression.outcome == ValidationOutcome.FAILED and regression.regression
    assert evaluate([_phase("patched", None, timed_out=True)], plan).outcome == ValidationOutcome.TIMED_OUT
    assert evaluate([_phase("patched", 5)], plan).outcome == ValidationOutcome.NOT_AVAILABLE
    assert evaluate([_phase("patched", None, error="no docker")], plan).outcome == ValidationOutcome.NOT_RUN
    assert evaluate([], TestPlan("python", None, notes=["none"])).outcome == ValidationOutcome.NOT_AVAILABLE
    failed_static = evaluate([PhaseResult("static", ["python"], ExecutionResult(1, "", "", 1),
                                          ValidationOutcome.FAILED)], plan)
    assert failed_static.outcome == ValidationOutcome.FAILED


def test_regression_detected_by_failing_test_ids():
    plan = TestPlan(ecosystem="python", image=None, has_tests=True)
    baseline = _phase("baseline", 1)
    baseline.result.stdout = "FAILED tests/test_a.py::test_new - ZeroDivisionError\n1 failed, 2 passed"
    patched = _phase("patched", 1)
    patched.result.stdout = "FAILED tests/test_a.py::test_discount - assert 15 == 85\n1 failed, 2 passed"
    verdict = evaluate([baseline, patched], plan)
    assert verdict.outcome == ValidationOutcome.FAILED and verdict.regression
    assert "tests/test_a.py::test_discount" in verdict.summary
    still = _phase("patched", 1)
    still.result.stdout = baseline.result.stdout
    assert not evaluate([baseline, still], plan).regression


def test_test_count_parsing():
    assert parse_test_counts("....\n4 passed in 0.03s") == (4, 0)
    assert parse_test_counts("1 failed, 3 passed, 1 error in 2s") == (3, 2)
    assert parse_test_counts("Tests:       1 failed, 3 passed, 4 total") == (3, 1)
    assert parse_test_counts("no summary") == (None, None)
