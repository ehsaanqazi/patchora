"""Sentry and GitHub payload parsing."""

from __future__ import annotations

import io
import tarfile

import pytest

from app.core.exceptions import ExternalServiceError, InvalidRequestError
from app.demo.scenarios import SCENARIOS
from app.security.input_sanitization import normalize_repo_path, validate_branch_name
from app.services.github_service import GitHubService, safe_extract_tarball
from app.services.http import parse_link_header
from app.services.sentry_service import parse_event, parse_issue


def test_parse_issue_and_event():
    scenario = SCENARIOS["zero-division"]
    issue = parse_issue(scenario.issue, "acme-demo")
    assert issue.event_count == 342 and issue.exception_type == "ZeroDivisionError"
    event = parse_event(scenario.event)
    assert event.release == "shop-api@1.4.2"
    exc = event.primary_exception
    assert exc is not None and exc.type == "ZeroDivisionError"
    assert [f.function for f in exc.frames] == ["merchant_summary", "average_order_value"]
    assert exc.frames[-1].context == [(19, "    return round(total / len(orders), 2)")]
    assert "vars" not in exc.frames[-1].model_dump()


def test_parse_event_release_from_tags_and_raw_format():
    event = parse_event({"event_id": "x", "tags": [["release", "v9"]],
                         "exception": {"values": [{"type": "KeyError", "value": "'a'",
                                                   "stacktrace": {"frames": [{"filename": "a.py", "lineno": 3,
                                                                              "in_app": True}]}}]}})
    assert event.release == "v9"
    assert event.primary_exception.frames[0].lineno == 3
    assert event.primary_exception.frames[0].in_app is True


def test_parse_event_never_invents_frames():
    event = parse_event({"eventID": "1", "entries": [{"type": "message", "data": {"formatted": "x"}}]})
    assert event.exceptions == [] and event.release is None


def test_malformed_payloads():
    with pytest.raises(ExternalServiceError):
        parse_issue({"title": "no id"}, "org")
    with pytest.raises(ExternalServiceError):
        parse_event(["not", "a", "dict"])  # type: ignore[arg-type]


def test_link_header():
    links = parse_link_header('<https://x/?cursor=a>; rel="previous"; results="false"; cursor="a", '
                              '<https://x/?cursor=b>; rel="next"; results="true"; cursor="b"')
    assert links["next"]["cursor"] == "b" and links["next"]["results"] == "true"


def test_pr_info_parsing():
    info = GitHubService._pr_info({"number": 3, "id": 99, "html_url": "https://github.com/a/b/pull/3",
                                   "state": "closed", "merged_at": "2026-01-01T00:00:00Z", "draft": False,
                                   "head": {"ref": "patchora/x", "sha": "abc"}, "base": {"ref": "main"}})
    assert info.merged and info.head_branch == "patchora/x"


@pytest.mark.parametrize("name", ["main", "patchora/issue-1/run-2", "feature/x.y"])
def test_valid_branch_names(name):
    assert validate_branch_name(name) == name


@pytest.mark.parametrize("name", ["", "-x", "a..b", "a//b", "a.lock", "a b", "a~1", "x/.hidden", "@", "a@{1}", "/x"])
def test_invalid_branch_names(name):
    with pytest.raises(InvalidRequestError):
        validate_branch_name(name)


@pytest.mark.parametrize("path", ["../x", "a/../../x", "/etc/passwd", "C:/x", "a\\b", ".git/config", "a\x00b"])
def test_unsafe_paths(path):
    with pytest.raises(InvalidRequestError):
        normalize_repo_path(path)


def _tar(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def test_safe_extract_rejects_traversal_and_links(tmp_path):
    good = tarfile.TarInfo("root/src/a.py")
    good.size = 3
    evil = tarfile.TarInfo("root/../../evil.py")
    evil.size = 1
    link = tarfile.TarInfo("root/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    hard = tarfile.TarInfo("root/hard")
    hard.type = tarfile.LNKTYPE
    hard.linkname = "root/src/a.py"
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(_tar([(good, b"abc"), (evil, b"x"), (link, None), (hard, None)]))
    dest = tmp_path / "out"
    skipped = safe_extract_tarball(archive, dest)
    assert (dest / "src" / "a.py").read_text() == "abc"
    assert not (tmp_path / "evil.py").exists()
    assert not (dest / "link").exists() and not (dest / "hard").exists()
    assert len(skipped) == 3
