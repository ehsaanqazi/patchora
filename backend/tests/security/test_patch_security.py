"""Malicious and out-of-policy patches are rejected regardless of what the model claims."""

from __future__ import annotations

import os

import pytest

from app.config import DEFAULT_PROTECTED_PATHS
from app.services.patch_service import (
    PatchError,
    PatchPolicy,
    apply_patch_to_workspace,
    build_unified_diff,
    validate_patch,
)


def policy(**kw) -> PatchPolicy:
    base = dict(max_files=10, max_lines=500, protected_paths=DEFAULT_PROTECTED_PATHS)
    base.update(kw)
    return PatchPolicy(**base)


def codes(diff: str, **kw) -> set[str]:
    return {f.code for f in validate_patch(diff, policy(**kw)).findings}


def raw(path_a: str, path_b: str, body: str = "@@ -1 +1 @@\n-a\n+b\n", extra: str = "") -> str:
    return f"diff --git a/{path_a} b/{path_b}\n{extra}--- a/{path_a}\n+++ b/{path_b}\n{body}"


@pytest.mark.parametrize("path", ["../outside.py", "src/../../etc/passwd", ".git/hooks/pre-commit"])
def test_path_traversal_rejected(path):
    assert "unsafe_path" in codes(raw(path, path))


def test_absolute_paths_rejected():
    diff = "diff --git a//etc/passwd b//etc/passwd\n--- /etc/passwd\n+++ /etc/passwd\n@@ -1 +1 @@\n-a\n+b\n"
    assert "unsafe_path" in codes(diff)


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "deploy/prod.yaml", "config/.env", "Dockerfile",
                                  "package.json", "requirements.txt", "infra/main.tf", "CODEOWNERS",
                                  "keys/server.pem"])
def test_protected_files_rejected(path):
    diff = build_unified_diff({path: "a\n"}, {path: "b\n"})
    assert "protected_path" in codes(diff)


def test_rename_bypass_of_protected_path_rejected():
    diff = raw("src/ok.py", ".github/workflows/ci.yml", extra="rename from src/ok.py\nrename to .github/workflows/ci.yml\n")
    found = codes(diff)
    assert "protected_path" in found and "rename" in found


def test_binary_mode_and_deletion_rejected():
    binary = "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
    assert "binary_change" in codes(binary)
    mode = raw("run.sh", "run.sh", extra="old mode 100644\nnew mode 100755\n")
    assert "mode_change" in codes(mode)
    exec_new = ("diff --git a/x.sh b/x.sh\nnew file mode 100755\n--- /dev/null\n+++ b/x.sh\n"
                "@@ -0,0 +1 @@\n+echo hi\n")
    assert "mode_change" in codes(exec_new)
    deletion = "diff --git a/a.py b/a.py\ndeleted file mode 100644\n--- a/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
    assert "file_deletion" in codes(deletion)


def test_symlinks_and_submodules_rejected():
    diff = build_unified_diff({"link/a.py": "a\n"}, {"link/a.py": "b\n"})
    assert "symlink" in codes(diff, symlink_paths=frozenset({"link"}))
    assert "submodule" in codes(diff, submodule_paths=frozenset({"link"}))


def test_oversized_patches_rejected():
    big = build_unified_diff({"a.py": ""}, {"a.py": "x = 1\n" * 600})
    assert "patch_too_large" in codes(big)
    with pytest.raises(PatchError):
        from app.services.patch_service import parse_unified_diff

        parse_unified_diff("diff --git a/a b/a\n" + "x" * 1_100_000)


@pytest.mark.parametrize("secret_line", [
    'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"',
    'token = "ghp_' + "a" * 36 + '"',
    "-----BEGIN RSA PRIVATE KEY-----",
    'api_key = "Zq8vB3nR7tY1uW9xK2mP5sD4"',
    "DATABASE_URL = 'postgres://admin:hunter2secret@db.internal:5432/app'",
])
def test_secret_leakage_rejected(secret_line):
    diff = build_unified_diff({"a.py": "x = 1\n"}, {"a.py": f"x = 1\n{secret_line}\n"})
    assert "secret_detected" in codes(diff)


def test_configured_secret_values_rejected_even_without_pattern():
    diff = build_unified_diff({"a.py": "x = 1\n"}, {"a.py": "x = 'plainvalue-xyz-123'\n"})
    assert "secret_detected" in codes(diff, known_secrets=["plainvalue-xyz-123"])
    assert "secret_detected" not in codes(diff)


def test_placeholders_are_not_flagged():
    diff = build_unified_diff({"a.py": "x = 1\n"}, {"a.py": 'password = "changeme-example-value"\n'})
    assert "secret_detected" not in codes(diff)


def test_unknown_files_rejected_when_tree_known():
    diff = build_unified_diff({"ghost.py": "a\n"}, {"ghost.py": "b\n"})
    assert "unknown_file" in codes(diff, existing_paths=frozenset({"real.py"}))


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_workspace_symlink_escape_blocked(tmp_path):
    workspace = tmp_path / "ws"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "target.py").write_text("a\n")
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks requires privileges on this system")
    diff = build_unified_diff({"link/target.py": "a\n"}, {"link/target.py": "pwned\n"})
    with pytest.raises(PatchError, match="symlink"):
        apply_patch_to_workspace(workspace, diff)
    assert (outside / "target.py").read_text() == "a\n"


def test_workspace_apply_rejects_traversal(tmp_path):
    diff = "diff --git a/../x.py b/../x.py\n--- a/../x.py\n+++ b/../x.py\n@@ -1 +1 @@\n-a\n+b\n"
    from app.core.exceptions import InvalidRequestError

    with pytest.raises((PatchError, InvalidRequestError)):
        apply_patch_to_workspace(tmp_path, diff)
