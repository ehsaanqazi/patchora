from __future__ import annotations

import pytest

from app.agent.tools import build_patch, extract_test_changes
from app.schemas.agent_outputs import PatchEdit, PatchProposal
from app.services.patch_service import (
    PatchError,
    PatchPolicy,
    apply_edits,
    apply_patch_to_texts,
    assess_risk,
    build_unified_diff,
    parse_unified_diff,
    validate_patch,
)

ORIGINAL = "def f(x):\n    return 1 / x\n\n\ndef g():\n    return 2\n"


def policy(**kw) -> PatchPolicy:
    base = dict(max_files=10, max_lines=500, protected_paths=[".github/**", "**/.env"])
    base.update(kw)
    return PatchPolicy(**base)


def test_apply_edits_requires_unique_match():
    with pytest.raises(PatchError, match="not found"):
        apply_edits({"a.py": ORIGINAL}, [PatchEdit(path="a.py", search="missing", replace="x")])
    with pytest.raises(PatchError, match="2 locations"):
        apply_edits({"a.py": "x\nx\n"}, [PatchEdit(path="a.py", search="x", replace="y")])
    with pytest.raises(PatchError, match="not retrieved"):
        apply_edits({}, [PatchEdit(path="b.py", search="x", replace="y")])


def test_apply_edits_and_create():
    updated = apply_edits({"a.py": ORIGINAL, "t.py": None}, [
        PatchEdit(path="a.py", search="    return 1 / x\n", replace="    return 1 / x if x else 0\n"),
        PatchEdit(path="t.py", action="create", replace="def test():\n    pass"),
    ])
    assert "if x else 0" in updated["a.py"]
    assert updated["t.py"].endswith("\n")
    with pytest.raises(PatchError, match="already exists"):
        apply_edits({"a.py": ORIGINAL}, [PatchEdit(path="a.py", action="create", replace="x")])


def test_diff_roundtrip_matches_edits():
    originals = {"a.py": ORIGINAL, "tests/test_a.py": None}
    proposal = PatchProposal(summary="fix", edits=[
        PatchEdit(path="a.py", search="    return 1 / x\n", replace="    if not x:\n        return 0\n    return 1 / x\n"),
        PatchEdit(path="tests/test_a.py", action="create", replace="def test_zero():\n    assert True\n"),
    ])
    diff, updated = build_patch(originals, proposal)
    files = parse_unified_diff(diff)
    assert {f.path for f in files} == {"a.py", "tests/test_a.py"}
    assert next(f for f in files if f.path == "tests/test_a.py").is_new
    assert apply_patch_to_texts(originals, files) == updated


def test_crlf_files_keep_line_endings():
    crlf = ORIGINAL.replace("\n", "\r\n")
    updated = apply_edits({"a.py": crlf}, [PatchEdit(path="a.py", search="return 2", replace="return 3")])
    diff = build_unified_diff({"a.py": crlf}, updated)
    result = apply_patch_to_texts({"a.py": crlf}, parse_unified_diff(diff))
    assert result["a.py"] == crlf.replace("return 2", "return 3")


def test_context_mismatch_is_rejected():
    diff = build_unified_diff({"a.py": ORIGINAL}, {"a.py": ORIGINAL.replace("return 2", "return 3")})
    with pytest.raises(PatchError, match="Context mismatch"):
        apply_patch_to_texts({"a.py": ORIGINAL.replace("def g", "def h")}, parse_unified_diff(diff))


@pytest.mark.parametrize("text", [
    "just some text",
    "--- a/x.py\n@@ -1 +1 @@\n-a\n+b\n",
    "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n-a\n+b\n",
    "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ bogus @@\n",
])
def test_malformed_patches(text):
    report = validate_patch(text, policy())
    assert report.blocked
    assert report.findings[0].code == "malformed_patch"


def test_size_limits_and_risk():
    big = {"a.py": "".join(f"line{i}\n" for i in range(50))}
    diff = build_unified_diff({"a.py": ""}, big)
    report = validate_patch(diff, policy(max_lines=10))
    assert any(f.code == "patch_too_large" for f in report.findings)
    many = {f"m{i}.py": "x\n" for i in range(4)}
    diff = build_unified_diff({p: "y\n" for p in many}, many)
    report = validate_patch(diff, policy(max_files=3))
    assert any(f.code == "too_many_files" for f in report.findings)
    ok = validate_patch(build_unified_diff({"a.py": "a\n"}, {"a.py": "b\n"}), policy())
    assert not ok.blocked
    assert assess_risk(ok, "low") == "low"
    assert assess_risk(ok, "high") == "high"


def test_extract_test_changes():
    diff = build_unified_diff({"shop/a.py": "a\n", "tests/test_a.py": "t\n"},
                              {"shop/a.py": "b\n", "tests/test_a.py": "u\n"})
    sub = extract_test_changes(diff)
    assert sub is not None
    assert [f.path for f in parse_unified_diff(sub)] == ["tests/test_a.py"]
    assert extract_test_changes(build_unified_diff({"a.py": "a\n"}, {"a.py": "b\n"})) is None
