"""Repository investigation engine.

Deterministic evidence gathering that runs *before* any model call:

1. Identify application-owned stack frames (``in_app`` or path heuristics).
2. Map frame paths to files in the pinned revision (mapping strip prefixes, exact match,
   module-path conversion, then unique longest-suffix match).
3. Fetch bounded excerpts around the failing lines and compare them with the source context
   Sentry captured, to detect when the repository revision differs from the deployed code.
4. Discover related tests, definitions and call sites in nearby files.
5. Optionally add semantic-search hits (FAISS) - never required, never used for authorization.

Everything collected here is *observed fact*; hypotheses come later from the model and must
cite these evidence ids.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import ExternalNotFound
from app.core.logging import get_logger
from app.schemas.issues import NormalizedEvent, StackFrame
from app.schemas.repositories import FileContent, TreeEntry
from app.security.input_sanitization import normalize_repo_path, path_matches
from app.services.github_service import CodeHost
from app.services.patch_service import is_test_path

logger = get_logger(__name__)

# Never fetched, never shown to the model.
SENSITIVE_PATTERNS = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx", "**/id_rsa*",
    "**/secrets/**", "**/.npmrc", "**/.pypirc", "**/credentials*", "**/*.keystore",
]
LIBRARY_MARKERS = ("site-packages/", "dist-packages/", "node_modules/", "/lib/python", "<frozen",
                   "/usr/lib/", "/usr/local/lib/", "webpack/runtime", "internal/")
SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb", ".java", ".kt",
                     ".php", ".rs", ".cs"}

MAX_FRAMES = 8
MAX_SOURCE_FILES = 12
MAX_TEST_FILES = 4
MAX_NEIGHBOR_FILES = 6
EXCERPT_RADIUS = 25


@dataclass
class EvidenceItem:
    id: str
    kind: str  # stack_frame | related_test | call_site | definition | dependency | semantic | requested
    path: str
    start_line: int
    end_line: int
    excerpt: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "path": self.path, "start_line": self.start_line,
                "end_line": self.end_line, "excerpt": self.excerpt, "reason": self.reason}


@dataclass
class ResolvedFrame:
    frame_index: int
    original_path: str | None
    repo_path: str | None
    function: str | None
    lineno: int | None
    match_method: str | None
    context_matches: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Investigation:
    revision: str
    frames: list[ResolvedFrame] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # path -> text (bounded)
    related_tests: list[str] = field(default_factory=list)
    known_test_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tree_paths: set[str] = field(default_factory=set)
    symlinks: set[str] = field(default_factory=set)
    submodules: set[str] = field(default_factory=set)
    file_modes: dict[str, str] = field(default_factory=dict)

    def evidence_ids(self) -> set[str]:
        return {e.id for e in self.evidence}

    def to_state(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "frames": [f.as_dict() for f in self.frames],
            "evidence": [e.as_dict() for e in self.evidence],
            "related_tests": self.related_tests,
            "known_test_files": self.known_test_files[:200],
            "notes": self.notes,
            "file_paths": sorted(self.files),
        }


def is_library_path(path: str | None) -> bool:
    return bool(path) and any(marker in (path or "") for marker in LIBRARY_MARKERS)


def is_sensitive(path: str) -> bool:
    return path_matches(path, SENSITIVE_PATTERNS) is not None


def application_frames(frames: list[StackFrame]) -> list[tuple[int, StackFrame]]:
    """Innermost-first list of application frames."""
    indexed = list(enumerate(frames))
    marked = [(i, f) for i, f in indexed if f.in_app is True]
    if not marked:
        marked = [(i, f) for i, f in indexed
                  if not is_library_path(f.abs_path or f.filename) and (f.filename or f.abs_path)]
    return list(reversed(marked))[:MAX_FRAMES]


class PathResolver:
    def __init__(self, tree_paths: set[str], strip_prefixes: list[str], path_prefixes: list[str]) -> None:
        self.paths = tree_paths
        self.strip = sorted(strip_prefixes, key=len, reverse=True)
        self.prefixes = path_prefixes
        self._by_basename: dict[str, list[str]] = {}
        for p in tree_paths:
            self._by_basename.setdefault(posixpath.basename(p), []).append(p)

    def _clean(self, raw: str) -> str:
        path = raw.replace("\\", "/")
        for prefix in self.strip:
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        path = re.sub(r"^(webpack|app|file)://+[^/]*/?", "", path)
        path = re.sub(r"^[A-Za-z]:/", "", path)
        return path.lstrip("./").lstrip("/")

    def resolve(self, frame: StackFrame) -> tuple[str | None, str | None]:
        candidates = [c for c in (frame.abs_path, frame.filename) if c]
        for raw in candidates:
            cleaned = self._clean(raw)
            if cleaned in self.paths:
                return cleaned, "exact"
            for prefix in self.prefixes:
                joined = posixpath.join(prefix.rstrip("/"), cleaned)
                if joined in self.paths:
                    return joined, "path_prefix"
        if frame.module and "." in frame.module and not frame.module.startswith("<"):
            module_path = frame.module.replace(".", "/")
            for suffix in (".py", "/__init__.py"):
                hit = self._suffix_match(module_path + suffix)
                if hit:
                    return hit, "module"
        for raw in candidates:
            hit = self._suffix_match(self._clean(raw))
            if hit:
                return hit, "suffix"
        return None, None

    def _suffix_match(self, cleaned: str) -> str | None:
        parts = [p for p in cleaned.split("/") if p]
        if not parts:
            return None
        options = self._by_basename.get(parts[-1], [])
        if not options:
            return None
        best: list[str] = []
        best_len = 0
        for option in options:
            opt_parts = option.split("/")
            n = 0
            while n < min(len(parts), len(opt_parts)) and parts[-1 - n] == opt_parts[-1 - n]:
                n += 1
            if n > best_len:
                best, best_len = [option], n
            elif n == best_len:
                best.append(option)
        if len(best) == 1:
            return best[0]
        if self.prefixes:
            preferred = [b for b in best if any(b.startswith(p) for p in self.prefixes)]
            if len(preferred) == 1:
                return preferred[0]
        return None  # ambiguous: never guess


def numbered_excerpt(text: str, center: int | None, radius: int = EXCERPT_RADIUS,
                     max_lines: int = 120) -> tuple[str, int, int]:
    lines = text.splitlines()
    if not lines:
        return "", 0, 0
    if center is None or center < 1 or center > len(lines):
        start, end = 1, min(len(lines), max_lines)
    else:
        start, end = max(1, center - radius), min(len(lines), center + radius)
    width = len(str(end))
    body = "\n".join(f"{n:>{width}} | {lines[n - 1][:300]}" for n in range(start, end + 1))
    return body, start, end


def related_test_candidates(source_path: str, tree_paths: set[str]) -> list[str]:
    stem, ext = posixpath.splitext(posixpath.basename(source_path))
    if stem == "__init__":
        stem = posixpath.basename(posixpath.dirname(source_path)) or stem
    names = {f"test_{stem}.py", f"{stem}_test.py", f"tests_{stem}.py", f"{stem}.test{ext}", f"{stem}.spec{ext}",
             f"{stem}.test.ts", f"{stem}.test.js", f"{stem}.spec.ts", f"{stem}.spec.js", f"{stem}_test.go"}
    hits = [p for p in tree_paths if posixpath.basename(p) in names]
    return sorted(hits, key=lambda p: (len(p), p))[:MAX_TEST_FILES]


def _find_symbol_lines(text: str, symbol: str) -> tuple[list[int], list[int]]:
    """Return (definition_lines, call_lines) for a symbol name."""
    definitions: list[int] = []
    calls: list[int] = []
    def_re = re.compile(rf"^\s*(?:async\s+)?(?:def|class|function)\s+{re.escape(symbol)}\b|"
                        rf"\b(?:const|let|var)\s+{re.escape(symbol)}\s*=|^\s*{re.escape(symbol)}\s*[:=]\s*(?:async\s*)?\(")
    call_re = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    for number, line in enumerate(text.splitlines(), start=1):
        if def_re.search(line):
            definitions.append(number)
        elif call_re.search(line):
            calls.append(number)
    return definitions, calls


class Investigator:
    def __init__(self, code_host: CodeHost, full_name: str, revision: str, *,
                 strip_prefixes: list[str], path_prefixes: list[str], budget_chars: int) -> None:
        self.host = code_host
        self.full_name = full_name
        self.revision = revision
        self.strip_prefixes = strip_prefixes
        self.path_prefixes = path_prefixes
        self.budget_chars = budget_chars
        self._counter = 0
        self._used_chars = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"E{self._counter}"

    def _add(self, inv: Investigation, kind: str, path: str, text: str, center: int | None, reason: str,
             radius: int = EXCERPT_RADIUS) -> EvidenceItem | None:
        excerpt, start, end = numbered_excerpt(text, center, radius)
        if not excerpt or self._used_chars + len(excerpt) > self.budget_chars:
            return None
        for existing in inv.evidence:  # dedupe overlapping excerpts of the same file
            if existing.path == path and existing.start_line <= start and existing.end_line >= end:
                return None
        self._used_chars += len(excerpt)
        item = EvidenceItem(self._next_id(), kind, path, start, end, excerpt, reason)
        inv.evidence.append(item)
        return item

    async def _fetch(self, inv: Investigation, path: str) -> str | None:
        if path in inv.files:
            return inv.files[path]
        if is_sensitive(path) or path in inv.symlinks or path not in inv.tree_paths:
            return None
        try:
            content: FileContent = await self.host.get_file(self.full_name, path, self.revision)
        except ExternalNotFound:
            inv.notes.append(f"{path} could not be retrieved at {self.revision[:12]}.")
            return None
        if content.text is None:
            reason = ("too large" if content.too_large else "binary" if content.binary else
                      "a symlink" if content.symlink else "unavailable")
            inv.notes.append(f"{path} was skipped ({reason}).")
            return None
        inv.files[path] = content.text
        return content.text

    async def run(self, event: NormalizedEvent) -> Investigation:
        inv = Investigation(revision=self.revision)
        tree: list[TreeEntry] = await self.host.get_tree(self.full_name, self.revision)
        for entry in tree:
            if entry.is_submodule:
                inv.submodules.add(entry.path)
            elif entry.type == "blob":
                if entry.is_symlink:
                    inv.symlinks.add(entry.path)
                inv.tree_paths.add(entry.path)
                inv.file_modes[entry.path] = entry.mode
        inv.known_test_files = sorted(p for p in inv.tree_paths if is_test_path(p))[:500]

        exception = event.primary_exception
        if exception is None or not exception.frames:
            inv.notes.append("The Sentry event has no stack trace; evidence is limited to the error message.")
            return inv

        resolver = PathResolver(inv.tree_paths, self.strip_prefixes, self.path_prefixes)
        for index, frame in application_frames(exception.frames):
            repo_path, method = resolver.resolve(frame)
            resolved = ResolvedFrame(index, frame.abs_path or frame.filename, repo_path, frame.function,
                                     frame.lineno, method)
            inv.frames.append(resolved)
            if repo_path is None:
                inv.notes.append(f"Frame '{frame.filename or frame.abs_path}' could not be mapped to a "
                                 "file in the repository.")
                continue
            if len(inv.files) >= MAX_SOURCE_FILES and repo_path not in inv.files:
                continue
            text = await self._fetch(inv, repo_path)
            if text is None:
                continue
            lines = text.splitlines()
            if frame.lineno and frame.context:
                reported = dict(frame.context).get(frame.lineno)
                if reported is not None and 0 < frame.lineno <= len(lines):
                    resolved.context_matches = lines[frame.lineno - 1].strip() == reported.strip()
                    if not resolved.context_matches:
                        inv.notes.append(
                            f"{repo_path}:{frame.lineno} differs from the source captured by Sentry; the "
                            "repository revision may not match the deployed code."
                        )
            if frame.lineno and frame.lineno > len(lines):
                inv.notes.append(f"{repo_path} has {len(lines)} lines but the frame reports line "
                                 f"{frame.lineno}; the revision likely differs from the deployed code.")
            self._add(inv, "stack_frame", repo_path, text, frame.lineno,
                      f"Stack frame #{index} in {frame.function or '?'} (line {frame.lineno})")

        source_paths = [f.repo_path for f in inv.frames if f.repo_path]
        # Related tests.
        for path in dict.fromkeys(source_paths):
            for test_path in related_test_candidates(path, inv.tree_paths):
                if test_path in inv.related_tests:
                    continue
                inv.related_tests.append(test_path)
                text = await self._fetch(inv, test_path)
                if text is not None:
                    self._add(inv, "related_test", test_path, text, None,
                              f"Existing tests for {path}", radius=60)

        # Definitions and call sites of the failing functions in nearby files.
        functions = [f.function for f in inv.frames if f.function and f.function.isidentifier()]
        neighbor_dirs = {posixpath.dirname(p) for p in source_paths}
        neighbors = sorted(
            p for p in inv.tree_paths
            if posixpath.dirname(p) in neighbor_dirs and posixpath.splitext(p)[1] in SOURCE_EXTENSIONS
            and p not in inv.files and not is_sensitive(p)
        )[:MAX_NEIGHBOR_FILES]
        for path in neighbors:
            await self._fetch(inv, path)
        for symbol in list(dict.fromkeys(functions))[:4]:
            for path, text in list(inv.files.items()):
                definitions, calls = _find_symbol_lines(text, symbol)
                for line in definitions[:1]:
                    self._add(inv, "definition", path, text, line, f"Definition of {symbol}", radius=12)
                for line in calls[:2]:
                    self._add(inv, "call_site", path, text, line, f"Call site of {symbol}", radius=6)

        # Dependency manifests (names only; never secrets).
        for manifest in ("requirements.txt", "package.json", "pyproject.toml"):
            if manifest in inv.tree_paths and len(inv.evidence) < 30:
                text = await self._fetch(inv, manifest)
                if text is not None:
                    self._add(inv, "dependency", manifest, text, None, "Dependency manifest", radius=40)
        return inv

    async def add_requested_files(self, inv: Investigation, paths: list[str]) -> list[str]:
        """Fetch files the model asked for. Paths are validated against the pinned tree."""
        added: list[str] = []
        for raw in paths[:5]:
            try:
                path = normalize_repo_path(raw)
            except Exception:  # noqa: S112 - invalid model suggestions are ignored
                continue
            if path not in inv.tree_paths or is_sensitive(path):
                inv.notes.append(f"Requested file '{path[:100]}' is not available.")
                continue
            text = await self._fetch(inv, path)
            if text is not None and self._add(inv, "requested", path, text, None,
                                              "Requested by the diagnosis step", radius=80):
                added.append(path)
        return added

    def add_semantic_hits(self, inv: Investigation, hits: list[dict[str, Any]]) -> None:
        for hit in hits:
            path = hit.get("path")
            if not path or path not in inv.tree_paths or is_sensitive(path):
                continue  # similarity search never grants access beyond the pinned tree
            text = hit.get("text") or ""
            start = int(hit.get("start_line") or 1)
            numbered = "\n".join(f"{start + i} | {line[:300]}" for i, line in enumerate(text.splitlines()))
            if self._used_chars + len(numbered) > self.budget_chars:
                break
            self._used_chars += len(numbered)
            inv.evidence.append(EvidenceItem(self._next_id(), "semantic", path, start,
                                             start + max(0, len(text.splitlines()) - 1), numbered,
                                             f"Semantic search hit (score {hit.get('score', 0):.2f})"))
