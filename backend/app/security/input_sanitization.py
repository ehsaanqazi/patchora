"""Validation of identifiers, paths and untrusted text."""

from __future__ import annotations

import fnmatch
import posixpath
import re
import unicodedata

from app.core.exceptions import InvalidRequestError

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20~^:?*\[\\\x7f]")
_SENTRY_ISSUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_owner(owner: str) -> str:
    if not _OWNER_RE.match(owner or ""):
        raise InvalidRequestError(f"Invalid repository owner: {owner!r}")
    return owner


def validate_repo_name(name: str) -> str:
    if not _REPO_NAME_RE.match(name or "") or name in {".", ".."}:
        raise InvalidRequestError(f"Invalid repository name: {name!r}")
    return name


def validate_full_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").split("/")
    if len(parts) != 2:
        raise InvalidRequestError("Repository must be in 'owner/name' form.")
    return validate_owner(parts[0]), validate_repo_name(parts[1])


def validate_slug(value: str, what: str = "identifier") -> str:
    if not _SLUG_RE.match(value or ""):
        raise InvalidRequestError(f"Invalid {what}: {value!r}")
    return value


def validate_sentry_issue_id(value: str) -> str:
    if not _SENTRY_ISSUE_ID_RE.match(value or ""):
        raise InvalidRequestError(f"Invalid Sentry issue id: {value!r}")
    return value


def is_valid_sha(value: str | None) -> bool:
    return bool(value) and bool(_SHA_RE.match(value or ""))


def validate_branch_name(name: str) -> str:
    """Validate against git's check-ref-format rules (conservatively)."""
    if not name or len(name) > 200:
        raise InvalidRequestError("Branch name is empty or too long.")
    if (
        name.startswith(("-", "/", "."))
        or name.endswith(("/", ".", ".lock"))
        or ".." in name
        or "//" in name
        or "@{" in name
        or name == "@"
        or _BRANCH_FORBIDDEN.search(name)
        or any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))
    ):
        raise InvalidRequestError(f"Invalid branch name: {name!r}")
    return name


def normalize_repo_path(path: str) -> str:
    """Normalize a repository-relative path and reject anything that could escape the root.

    Raises InvalidRequestError for absolute paths, drive letters, traversal, NUL bytes,
    backslashes and paths inside ``.git``.
    """
    if not path or "\x00" in path:
        raise InvalidRequestError("Empty or NUL-containing path.")
    if "\\" in path:
        raise InvalidRequestError(f"Backslashes are not allowed in paths: {path!r}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise InvalidRequestError(f"Absolute paths are not allowed: {path!r}")
    if any(unicodedata.category(c) in {"Cc", "Cf"} for c in path):
        raise InvalidRequestError(f"Control characters are not allowed in paths: {path!r}")
    parts = path.split("/")
    if any(part == ".." for part in parts):
        raise InvalidRequestError(f"Parent-directory traversal is not allowed: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized in {".", ""} or normalized.startswith("../"):
        raise InvalidRequestError(f"Invalid path: {path!r}")
    if normalized == ".git" or normalized.startswith(".git/"):
        raise InvalidRequestError("Paths inside .git are not allowed.")
    if len(normalized) > 400:
        raise InvalidRequestError("Path too long.")
    return normalized


def validate_glob_pattern(pattern: str) -> str:
    pattern = pattern.strip()
    if not pattern or len(pattern) > 200 or "\x00" in pattern or "\\" in pattern:
        raise ValueError(f"invalid path pattern: {pattern!r}")
    return pattern


def path_matches(path: str, patterns: list[str]) -> str | None:
    """Return the first glob pattern matching ``path`` (``**`` matches any depth)."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return pattern
        if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
            return pattern
        if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
            return pattern
    return None


def truncate(text: str | None, limit: int, marker: str = "\n...[truncated]") -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


_UNTRUSTED_TAG = re.compile(r"</?\s*untrusted_data[^>]*>", re.IGNORECASE)


def wrap_untrusted(source: str, content: str, limit: int = 20_000) -> str:
    """Wrap external data for inclusion in a prompt.

    The content cannot close the wrapper early: any wrapper-like tags inside it are
    neutralised. The system prompt instructs the model that nothing inside these blocks is
    an instruction.
    """
    safe = _UNTRUSTED_TAG.sub("[tag removed]", truncate(content, limit))
    safe_source = re.sub(r"[^A-Za-z0-9_.:/ -]", "_", source)[:200]
    return f'<untrusted_data source="{safe_source}">\n{safe}\n</untrusted_data>'
