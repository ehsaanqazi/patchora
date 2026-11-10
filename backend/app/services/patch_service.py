"""Patch construction, parsing, application and policy validation.

Flow:
1. The model proposes search/replace edits (:class:`PatchEdit`).
2. :func:`apply_edits` applies them to the files fetched at the pinned base revision
   (each search block must match exactly once).
3. :func:`build_unified_diff` turns the result into a unified diff deterministically.
4. :func:`validate_patch` parses that diff and enforces every policy (paths, protected
   files, size, binary/mode changes, symlinks, secrets).
5. :func:`apply_patch_to_workspace` applies the validated diff to the disposable workspace
   with strict context matching and symlink-escape checks.

The model's own claims about safety are never consulted.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.exceptions import InvalidRequestError
from app.schemas.agent_outputs import PatchEdit
from app.security.input_sanitization import normalize_repo_path, path_matches
from app.security.secret_scanner import scan_text

MAX_PATCH_BYTES = 1_000_000


class PatchError(Exception):
    """A patch is malformed or cannot be applied."""


# --- Edits -> files -----------------------------------------------------------------------

def apply_edits(originals: dict[str, str | None], edits: list[PatchEdit]) -> dict[str, str]:
    """Apply search/replace edits. ``originals`` maps path -> current text (None if absent)."""
    updated: dict[str, str] = {}
    for edit in edits:
        try:
            path = normalize_repo_path(edit.path)
        except InvalidRequestError as exc:
            raise PatchError(str(exc.message)) from exc
        current = updated.get(path, originals.get(path))
        if edit.action == "create":
            if current is not None:
                raise PatchError(f"{path} already exists; use a 'modify' edit.")
            updated[path] = edit.replace if edit.replace.endswith("\n") else edit.replace + "\n"
            continue
        if current is None:
            raise PatchError(f"{path} was not retrieved or does not exist at the base revision.")
        if not edit.search:
            raise PatchError(f"Edit for {path} has an empty search block.")
        occurrences = current.count(edit.search)
        if occurrences == 0:
            # Tolerate CRLF/LF differences only; no fuzzy matching.
            normalized = current.replace("\r\n", "\n")
            if normalized.count(edit.search.replace("\r\n", "\n")) == 1:
                current = normalized
                occurrences = 1
                edit = edit.model_copy(update={"search": edit.search.replace("\r\n", "\n")})
            else:
                raise PatchError(f"Search block not found in {path}.")
        if occurrences > 1:
            raise PatchError(f"Search block matches {occurrences} locations in {path}; it must be unique.")
        updated[path] = current.replace(edit.search, edit.replace, 1)
    return {p: t for p, t in updated.items() if t != originals.get(p)}


def build_unified_diff(originals: dict[str, str | None], updated: dict[str, str]) -> str:
    chunks: list[str] = []
    for path in sorted(updated):
        before = originals.get(path)
        after = updated[path]
        a_lines = (before or "").splitlines(keepends=True)
        b_lines = after.splitlines(keepends=True)
        if a_lines and not a_lines[-1].endswith("\n"):
            a_lines[-1] += "\n"
        if b_lines and not b_lines[-1].endswith("\n"):
            b_lines[-1] += "\n"
        from_file = "/dev/null" if before is None else f"a/{path}"
        header = f"diff --git a/{path} b/{path}\n"
        if before is None:
            header += "new file mode 100644\n"
        diff = list(difflib.unified_diff(a_lines, b_lines, fromfile=from_file, tofile=f"b/{path}", n=3))
        if diff:
            chunks.append(header + "".join(diff))
    return "".join(chunks)


# --- Parsing ------------------------------------------------------------------------------

@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '+', '-' or '\\'


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    is_rename: bool = False
    mode_change: str | None = None
    new_file_mode: str | None = None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for line in h.lines if line.startswith("-"))


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(raw: str) -> str | None:
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw


def parse_unified_diff(text: str) -> list[FilePatch]:
    if len(text.encode("utf-8", errors="replace")) > MAX_PATCH_BYTES:
        raise PatchError("Patch exceeds the maximum size.")
    lines = text.splitlines()
    files: list[FilePatch] = []
    current: FilePatch | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            current = FilePatch(old_path=None, new_path=None)
            files.append(current)
            parts = line.split(" ")
            if len(parts) >= 4:
                current.old_path = _strip_prefix(parts[2])
                current.new_path = _strip_prefix(parts[3])
            i += 1
            continue
        if line.startswith(("old mode ", "new mode ")):
            if current is None:
                raise PatchError("Mode line outside a file section.")
            current.mode_change = line.split()[-1]
        elif line.startswith("new file mode "):
            if current is None:
                raise PatchError("Mode line outside a file section.")
            current.is_new = True
            current.new_file_mode = line.split()[-1]
        elif line.startswith("deleted file mode "):
            if current is None:
                raise PatchError("Mode line outside a file section.")
            current.is_deleted = True
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            if current is None:
                raise PatchError("Rename line outside a file section.")
            current.is_rename = True
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            if current is None:
                current = FilePatch(old_path=None, new_path=None)
                files.append(current)
            current.is_binary = True
        elif line.startswith("--- "):
            if current is None or current.hunks:
                current = FilePatch(old_path=None, new_path=None)
                files.append(current)
            current.old_path = _strip_prefix(line[4:])
            if current.old_path is None:
                current.is_new = True
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                raise PatchError("'---' header without a matching '+++' header.")
            current.new_path = _strip_prefix(lines[i + 1][4:])
            if current.new_path is None:
                current.is_deleted = True
            i += 2
            continue
        elif line.startswith("@@"):
            if current is None:
                raise PatchError("Hunk without a file header.")
            match = _HUNK_RE.match(line)
            if not match:
                raise PatchError(f"Malformed hunk header: {line[:80]}")
            hunk = Hunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2)) if match.group(2) is not None else 1,
                new_start=int(match.group(3)),
                new_count=int(match.group(4)) if match.group(4) is not None else 1,
            )
            i += 1
            old_seen = new_seen = 0
            while i < len(lines) and (old_seen < hunk.old_count or new_seen < hunk.new_count):
                body = lines[i]
                if body.startswith("\\"):
                    hunk.lines.append(body)
                    i += 1
                    continue
                tag = body[:1] if body else " "
                if tag not in {" ", "+", "-"}:
                    raise PatchError(f"Malformed hunk line: {body[:80]}")
                if tag in {" ", "-"}:
                    old_seen += 1
                if tag in {" ", "+"}:
                    new_seen += 1
                hunk.lines.append(body if body else " ")
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                hunk.lines.append(lines[i])
                i += 1
            if old_seen != hunk.old_count or new_seen != hunk.new_count:
                raise PatchError("Hunk line counts do not match its header.")
            current.hunks.append(hunk)
            continue
        elif line.strip() and current is None and not line.startswith(("index ", "similarity ")):
            raise PatchError(f"Unexpected content outside a file section: {line[:80]}")
        i += 1
    if not files:
        raise PatchError("Patch contains no file changes.")
    for fp in files:
        if not fp.is_binary and not fp.hunks and not fp.is_rename and not fp.mode_change:
            raise PatchError(f"File section for {fp.path or '?'} has no hunks.")
    return files


# --- Applying -----------------------------------------------------------------------------

def apply_file_patch(original: str | None, fp: FilePatch) -> str:
    """Apply hunks with exact context matching (no fuzz)."""
    source = (original or "").splitlines(keepends=False)
    trailing_newline = original is None or original.endswith("\n") or original == ""
    result: list[str] = []
    cursor = 0
    for hunk in fp.hunks:
        start = hunk.old_start - 1 if hunk.old_count > 0 else hunk.old_start
        if start < cursor or start > len(source):
            raise PatchError(f"Hunk for {fp.path} is out of range.")
        result.extend(source[cursor:start])
        pos = start
        no_newline_after_new = False
        for idx, line in enumerate(hunk.lines):
            if line.startswith("\\"):
                prev = hunk.lines[idx - 1] if idx > 0 else ""
                if prev.startswith(("+", " ")):
                    no_newline_after_new = True
                continue
            tag, content = line[0], line[1:]
            if tag in {" ", "-"}:
                if pos >= len(source) or source[pos] != content:
                    raise PatchError(f"Context mismatch in {fp.path} at line {pos + 1}; "
                                     "the file differs from the expected base revision.")
                pos += 1
            if tag in {" ", "+"}:
                result.append(content)
        cursor = pos
        if no_newline_after_new:
            trailing_newline = False
    result.extend(source[cursor:])
    newline = "\r\n" if original and "\r\n" in original else "\n"
    text = newline.join(result)
    if result and trailing_newline:
        text += newline
    return text


def apply_patch_to_texts(originals: dict[str, str | None], files: list[FilePatch]) -> dict[str, str]:
    out: dict[str, str] = {}
    for fp in files:
        if fp.is_binary or fp.is_deleted or fp.is_rename:
            raise PatchError(f"Unsupported change type for {fp.path}.")
        out[fp.path] = apply_file_patch(originals.get(fp.path), fp)
    return out


def _check_no_symlink_components(root: Path, relative: str) -> Path:
    root = root.resolve()
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise PatchError(f"Refusing to write through symlink: {relative}")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise PatchError(f"Path escapes the workspace: {relative}")
    return target


def apply_patch_to_workspace(root: Path, patch_text: str) -> dict[str, str]:
    """Apply a validated patch inside ``root``. Returns path -> new content."""
    files = parse_unified_diff(patch_text)
    originals: dict[str, str | None] = {}
    targets: dict[str, Path] = {}
    for fp in files:
        path = normalize_repo_path(fp.path)
        target = _check_no_symlink_components(root, path)
        targets[path] = target
        if fp.is_new:
            if target.exists():
                raise PatchError(f"{path} already exists in the workspace.")
            originals[path] = None
        else:
            if not target.is_file():
                raise PatchError(f"{path} does not exist in the workspace.")
            originals[path] = target.read_text(encoding="utf-8")
    new_contents = apply_patch_to_texts(originals, files)
    for path, content in new_contents.items():
        targets[path].parent.mkdir(parents=True, exist_ok=True)
        targets[path].write_text(content, encoding="utf-8", newline="")
    return new_contents


# --- Policy validation --------------------------------------------------------------------

@dataclass(frozen=True)
class PatchPolicy:
    max_files: int
    max_lines: int
    protected_paths: list[str]
    known_secrets: list[str] = field(default_factory=list)
    symlink_paths: frozenset[str] = frozenset()
    submodule_paths: frozenset[str] = frozenset()
    existing_paths: frozenset[str] | None = None


@dataclass
class PolicyFinding:
    code: str
    severity: str  # block | warn
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "path": self.path}


@dataclass
class PatchReport:
    files: list[FilePatch]
    findings: list[PolicyFinding]
    lines_added: int
    lines_removed: int

    @property
    def blocked(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


def validate_patch(patch_text: str, policy: PatchPolicy) -> PatchReport:
    findings: list[PolicyFinding] = []
    try:
        files = parse_unified_diff(patch_text)
    except PatchError as exc:
        return PatchReport([], [PolicyFinding("malformed_patch", "block", str(exc))], 0, 0)

    added_text: list[str] = []
    for fp in files:
        for raw in {fp.old_path, fp.new_path} - {None}:
            assert raw is not None
            try:
                normalized = normalize_repo_path(raw)
            except InvalidRequestError as exc:
                findings.append(PolicyFinding("unsafe_path", "block", exc.message, raw))
                continue
            if normalized != raw:
                findings.append(PolicyFinding("unsafe_path", "block", "Path is not normalized.", raw))
            protected = path_matches(normalized, policy.protected_paths)
            if protected:
                findings.append(PolicyFinding("protected_path", "block",
                                              f"Matches protected pattern '{protected}'.", normalized))
            if normalized in policy.symlink_paths or any(
                normalized.startswith(s + "/") for s in policy.symlink_paths
            ):
                findings.append(PolicyFinding("symlink", "block", "Path is or traverses a symlink.", normalized))
            if normalized in policy.submodule_paths or any(
                normalized.startswith(s + "/") for s in policy.submodule_paths
            ):
                findings.append(PolicyFinding("submodule", "block", "Path is inside a submodule.", normalized))
        if fp.is_binary:
            findings.append(PolicyFinding("binary_change", "block", "Binary changes are not allowed.", fp.path))
        if fp.is_deleted:
            findings.append(PolicyFinding("file_deletion", "block", "File deletions are not allowed.", fp.path))
        if fp.is_rename:
            findings.append(PolicyFinding("rename", "block", "Renames/copies are not allowed.", fp.path))
        if fp.mode_change or (fp.new_file_mode and fp.new_file_mode != "100644"):
            findings.append(PolicyFinding("mode_change", "block",
                                          "File mode/executable-bit changes are not allowed.", fp.path))
        if (
            not fp.is_new
            and policy.existing_paths is not None
            and fp.path not in policy.existing_paths
        ):
            findings.append(PolicyFinding("unknown_file", "block",
                                          "Modified file does not exist at the base revision.", fp.path))
        if fp.is_new and policy.existing_paths is not None and fp.path in policy.existing_paths:
            findings.append(PolicyFinding("file_exists", "block", "New file already exists.", fp.path))
        for hunk in fp.hunks:
            added_text.extend(line[1:] for line in hunk.lines if line.startswith("+"))

    if len(files) > policy.max_files:
        findings.append(PolicyFinding("too_many_files", "block",
                                      f"Patch touches {len(files)} files (limit {policy.max_files})."))
    added = sum(fp.added for fp in files)
    removed = sum(fp.removed for fp in files)
    if added + removed > policy.max_lines:
        findings.append(PolicyFinding("patch_too_large", "block",
                                      f"Patch changes {added + removed} lines (limit {policy.max_lines})."))
    for secret in scan_text("\n".join(added_text), policy.known_secrets):
        findings.append(PolicyFinding("secret_detected", "block",
                                      f"Possible credential in added lines ({secret.kind})."))
    return PatchReport(files=files, findings=findings, lines_added=added, lines_removed=removed)


TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|"
                          r"\.(test|spec)\.[jt]sx?$")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def assess_risk(report: PatchReport, model_risk: str | None) -> str:
    source_files = [p for p in report.paths if not is_test_path(p)]
    total = report.lines_added + report.lines_removed
    if report.blocked or model_risk == "high" or len(source_files) > 3 or total > 200:
        return "high"
    if model_risk == "medium" or len(source_files) > 1 or total > 60:
        return "medium"
    return "low"


def summarize_diff(report: PatchReport) -> str:
    parts = [f"{fp.path} (+{fp.added}/-{fp.removed}{', new' if fp.is_new else ''})" for fp in report.files]
    return f"{len(report.files)} file(s), +{report.lines_added}/-{report.lines_removed}: " + ", ".join(parts)
