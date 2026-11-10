"""Validation planning and execution.

Test commands come only from:
1. commands an admin configured for the repository (validated argv lists, no shell), or
2. fixed templates selected by detecting project conventions in the workspace
   (pytest for Python projects, ``npm test`` for Node projects with a test script).

The model may only *select test files* to run first; those are validated to be existing
test files inside the workspace before being appended to a templated command. Commands
from issues, READMEs or model output are never executed.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.core.enums import ValidationOutcome
from app.sandbox.interface import Sandbox
from app.sandbox.limits import SandboxLimits, validate_command_argv
from app.sandbox.result import ExecutionResult, parse_test_counts
from app.security.input_sanitization import normalize_repo_path
from app.services.patch_service import TEST_PATH_RE

PYTEST_BASE = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--color=no"]
PYTEST_NO_TESTS_COLLECTED = 5
_NPM_PLACEHOLDER_TEST = re.compile(r"no test specified", re.IGNORECASE)


@dataclass
class TestPlan:
    __test__ = False
    ecosystem: str  # python | node | unknown
    image: str | None
    setup: list[list[str]] = field(default_factory=list)
    targeted: list[list[str]] = field(default_factory=list)
    full: list[list[str]] = field(default_factory=list)
    static: list[list[str]] = field(default_factory=list)
    has_tests: bool = False
    source: str = "conventions"  # conventions | repository_config
    notes: list[str] = field(default_factory=list)


def _find_test_files(root: Path, limit: int = 500) -> list[str]:
    found: list[str] = []
    for path in root.rglob("*"):
        if len(found) >= limit:
            break
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".sandbox-", "node_modules/", ".git/", ".venv/", "venv/")) or "/node_modules/" in rel:
            continue
        if TEST_PATH_RE.search(rel) and path.suffix in {".py", ".js", ".ts", ".jsx", ".tsx", ".go"}:
            found.append(rel)
    return found


def validate_test_targets(root: Path, targets: list[str], known_tests: list[str]) -> list[str]:
    """Keep only model-suggested targets that are existing test files in the workspace."""
    valid: list[str] = []
    known = set(known_tests)
    for target in targets[:10]:
        try:
            rel = normalize_repo_path(target.split("::", 1)[0])
        except Exception:  # noqa: S112 - invalid suggestions are simply dropped
            continue
        if rel in known and (root / rel).is_file() and not rel.startswith("-"):
            valid.append(rel)
    return sorted(set(valid))


def detect_test_plan(
    root: Path,
    *,
    repo_commands: list[dict[str, object]] | None,
    test_targets: list[str],
    changed_files: list[str],
    python_image: str,
    node_image: str,
    allow_dependency_install: bool,
) -> TestPlan:
    test_files = _find_test_files(root)
    py_changed = [f for f in changed_files if f.endswith(".py")]
    js_changed = [f for f in changed_files if f.endswith((".js", ".mjs", ".cjs"))]

    if repo_commands:
        plan = TestPlan(ecosystem="configured", image=python_image, source="repository_config",
                        has_tests=True)
        for command in repo_commands:
            raw_argv = command.get("argv", [])
            argv = validate_command_argv([str(a) for a in raw_argv] if isinstance(raw_argv, list) else [])
            phase = str(command.get("phase", "test"))
            if argv[0] in {"npm", "npx", "node", "yarn", "pnpm"}:
                plan.image = node_image
            {"setup": plan.setup, "static": plan.static}.get(phase, plan.full).append(argv)
        if not allow_dependency_install:
            plan.setup = []
        return plan

    package_json = root / "package.json"
    python_markers = [root / n for n in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini",
                                         "requirements.txt", "setup.py")]
    py_tests = [t for t in test_files if t.endswith(".py")]
    if py_tests or (any(p.exists() for p in python_markers) and not package_json.exists()):
        plan = TestPlan(ecosystem="python", image=python_image)
        plan.has_tests = bool(py_tests)
        if allow_dependency_install:
            install = ["pip", "install", "--only-binary=:all:", "--target", ".sandbox-deps", "pytest"]
            for req in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
                if (root / req).is_file():
                    install += ["-r", req]
            plan.setup.append(install)
        if plan.has_tests:
            targets = validate_test_targets(root, test_targets, py_tests)
            if targets:
                plan.targeted.append([*PYTEST_BASE, *targets])
            plan.full.append(list(PYTEST_BASE))
        else:
            plan.notes.append("No Python test files were found in the repository.")
        if py_changed:
            plan.static.append(["python", "-m", "py_compile", *py_changed])
        return plan

    if package_json.is_file():
        plan = TestPlan(ecosystem="node", image=node_image)
        try:
            scripts = (json.loads(package_json.read_text(encoding="utf-8")).get("scripts") or {})
        except (ValueError, OSError):
            scripts = {}
            plan.notes.append("package.json could not be parsed.")
        test_script = scripts.get("test") if isinstance(scripts, dict) else None
        plan.has_tests = bool(test_script) and not _NPM_PLACEHOLDER_TEST.search(str(test_script))
        if allow_dependency_install:
            lock = (root / "package-lock.json").is_file()
            plan.setup.append(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"] if lock
                              else ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"])
        if plan.has_tests:
            plan.full.append(["npm", "test", "--silent"])
        else:
            plan.notes.append("package.json has no usable test script.")
        for f in js_changed:
            plan.static.append(["node", "--check", f])
        return plan

    # No recognised project configuration: still run syntax checks for the changed files.
    image = python_image if py_changed or not js_changed else node_image
    plan = TestPlan(ecosystem="unknown", image=image)
    plan.notes.append("No supported test configuration was detected.")
    if py_changed:
        plan.static.append(["python", "-m", "py_compile", *py_changed])
    elif js_changed:
        plan.static.extend(["node", "--check", f] for f in js_changed)
    return plan


def copy_workspace(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest, symlinks=False, ignore_dangling_symlinks=True)


@dataclass
class PhaseResult:
    phase: str
    command: list[str]
    result: ExecutionResult
    outcome: ValidationOutcome
    tests_passed: int | None = None
    tests_failed: int | None = None


def classify(result: ExecutionResult, *, is_pytest: bool) -> ValidationOutcome:
    if result.error is not None:
        return ValidationOutcome.NOT_RUN
    if result.timed_out:
        return ValidationOutcome.TIMED_OUT
    if result.exit_code == 0:
        return ValidationOutcome.PASSED
    if is_pytest and result.exit_code == PYTEST_NO_TESTS_COLLECTED:
        return ValidationOutcome.NOT_AVAILABLE
    return ValidationOutcome.FAILED


async def run_phase(sandbox: Sandbox, workspace: Path, phase: str, command: list[str],
                    limits: SandboxLimits, image: str | None) -> PhaseResult:
    result = await sandbox.run(workspace, command, limits, image=image)
    outcome = classify(result, is_pytest="pytest" in command)
    if phase == "setup" and outcome not in {ValidationOutcome.PASSED, ValidationOutcome.NOT_RUN}:
        outcome = ValidationOutcome.SETUP_FAILED
    passed, failed = parse_test_counts(result.stdout + "\n" + result.stderr) if phase != "setup" else (None, None)
    return PhaseResult(phase, command, result, outcome, passed, failed)


@dataclass
class ValidationVerdict:
    outcome: ValidationOutcome
    reproduced: bool  # baseline (with new tests) failed and patched run passed
    regression: bool  # tests that passed before the patch fail after it
    summary: str


def evaluate(phases: list[PhaseResult], plan: TestPlan) -> ValidationVerdict:
    setup = [p for p in phases if p.phase == "setup"]
    baseline = [p for p in phases if p.phase == "baseline"]
    patched = [p for p in phases if p.phase == "patched"]
    static = [p for p in phases if p.phase == "static"]

    for p in setup:
        if p.outcome == ValidationOutcome.SETUP_FAILED:
            return ValidationVerdict(ValidationOutcome.SETUP_FAILED, False, False,
                                     "Dependency setup failed; tests could not be run.")
        if p.outcome == ValidationOutcome.NOT_RUN:
            return ValidationVerdict(ValidationOutcome.NOT_RUN, False, False,
                                     p.result.error or "Sandbox unavailable.")
    if any(p.outcome == ValidationOutcome.NOT_RUN for p in patched + static):
        reason = next((p.result.error for p in patched + static if p.result.error), "Sandbox unavailable.")
        return ValidationVerdict(ValidationOutcome.NOT_RUN, False, False, reason or "Sandbox unavailable.")
    if any(p.outcome == ValidationOutcome.TIMED_OUT for p in patched + static):
        return ValidationVerdict(ValidationOutcome.TIMED_OUT, False, False, "Validation timed out.")
    static_failed = any(p.outcome == ValidationOutcome.FAILED for p in static)

    if not patched:
        if static_failed:
            return ValidationVerdict(ValidationOutcome.FAILED, False, False,
                                     "Static checks failed on the patched files.")
        note = "; ".join(plan.notes) or "No tests are available."
        suffix = " Static checks passed." if static else ""
        return ValidationVerdict(ValidationOutcome.NOT_AVAILABLE, False, False, note + suffix)

    final = patched[-1]
    if final.outcome == ValidationOutcome.NOT_AVAILABLE and not static_failed:
        return ValidationVerdict(ValidationOutcome.NOT_AVAILABLE, False, False, "No tests were collected.")
    patched_ok = all(p.outcome == ValidationOutcome.PASSED for p in patched) and not static_failed
    baseline_failed = any(p.outcome == ValidationOutcome.FAILED for p in baseline)
    baseline_passed = bool(baseline) and all(p.outcome == ValidationOutcome.PASSED for p in baseline)
    if patched_ok:
        if baseline_failed:
            return ValidationVerdict(ValidationOutcome.PASSED, True, False,
                                     "Tests failed before the fix and pass after it.")
        return ValidationVerdict(ValidationOutcome.PASSED, False, False,
                                 "Tests pass with the patch (the failure was not reproduced by a test).")
    # Compare failing test ids when the same command ran on both sides: a test that passed at
    # baseline and fails with the patch is a regression even if the baseline also had failures.
    newly_failing: set[str] = set()
    if baseline and patched and baseline[0].command == patched[0].command:
        before = failing_test_ids(baseline[0].result.stdout)
        after = failing_test_ids(patched[0].result.stdout)
        newly_failing = after - before
    regression = baseline_passed or bool(newly_failing)
    detail = "Static checks failed. " if static_failed else ""
    if newly_failing:
        detail += f"Previously passing tests now fail (regression): {', '.join(sorted(newly_failing)[:5])}."
    elif regression:
        detail += "Tests that passed before the patch fail after it (regression)."
    else:
        detail += "Tests still fail after the patch."
    return ValidationVerdict(ValidationOutcome.FAILED, False, regression, detail)


_FAILED_ID = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)", re.MULTILINE)


def failing_test_ids(output: str) -> set[str]:
    """Failing pytest node ids from the short test summary (best effort, informational)."""
    return set(_FAILED_ID.findall(output))
