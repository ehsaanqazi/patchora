"""Code-host data models and repository API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ApiModel
from app.security.input_sanitization import validate_glob_pattern


class RepoInfo(BaseModel):
    provider_repository_id: str
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool = True
    permissions: dict[str, bool] = Field(default_factory=dict)


class CommitInfo(BaseModel):
    sha: str
    tree_sha: str
    message: str = ""
    date: datetime | None = None


class TreeEntry(BaseModel):
    path: str
    type: str  # blob | tree | commit (submodule)
    mode: str
    size: int | None = None
    sha: str | None = None

    @property
    def is_symlink(self) -> bool:
        return self.mode == "120000"

    @property
    def is_submodule(self) -> bool:
        return self.type == "commit" or self.mode == "160000"


class FileContent(BaseModel):
    path: str
    text: str | None = None
    size: int = 0
    binary: bool = False
    too_large: bool = False
    symlink: bool = False
    sha: str | None = None


class PullRequestInfo(BaseModel):
    number: int
    id: str
    url: str
    state: str
    merged: bool = False
    draft: bool = False
    head_branch: str
    base_branch: str
    head_sha: str | None = None
    title: str = ""


class IssueInfo(BaseModel):
    number: int
    id: str
    url: str
    state: str
    title: str


# --- API schemas ---------------------------------------------------------------------------

class TestCommand(BaseModel):
    """A trusted validation command configured by an admin, as an argv list (no shell)."""

    __test__ = False
    argv: list[str] = Field(min_length=1, max_length=30)
    phase: str = "test"  # setup | test | static

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: list[str]) -> list[str]:
        from app.sandbox.limits import validate_command_argv

        return validate_command_argv(value)


class RepositoryCreate(BaseModel):
    full_name: str = Field(max_length=201)
    enabled: bool = False


class RepositoryUpdate(BaseModel):
    enabled: bool | None = None
    test_commands: list[TestCommand] | None = None


class MappingIn(BaseModel):
    sentry_organization: str = Field(max_length=100)
    sentry_project: str = Field(max_length=100)
    branch: str | None = Field(default=None, max_length=200)
    path_prefixes: list[str] = Field(default_factory=list, max_length=20)
    strip_prefixes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("path_prefixes", "strip_prefixes")
    @classmethod
    def _validate_prefixes(cls, value: list[str]) -> list[str]:
        return [validate_glob_pattern(v) for v in value]


class MappingOut(ApiModel):
    id: int
    sentry_organization: str
    sentry_project: str
    repository_id: int
    branch: str | None
    path_prefixes: list[str]
    strip_prefixes: list[str]


class RepositoryOut(ApiModel):
    id: int
    provider: str
    provider_repository_id: str
    owner: str
    name: str
    full_name: str
    default_branch: str
    installation_id: str | None
    private: bool
    enabled: bool
    is_demo: bool
    test_commands: list[dict[str, Any]] | None
    last_synced_at: datetime | None
    created_at: datetime
    mappings: list[MappingOut] = Field(default_factory=list)


class RemoteRepositoryOut(ApiModel):
    full_name: str
    default_branch: str
    private: bool
    connected_id: int | None = None
