"""Repositories (allowlist), Sentry project mappings and trusted test commands."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.serializers import mapping_out, repository_out
from app.core.exceptions import ConflictError, InvalidRequestError, NotFoundError
from app.db.base import utcnow
from app.db.models import Repository, RepositoryMapping
from app.dependencies import Pagination, admin, current_principal, db_dep, factory_dep
from app.providers.factory import ProviderFactory
from app.schemas.common import Page
from app.schemas.integrations import ConnectionTestResult
from app.schemas.repositories import (
    MappingIn,
    MappingOut,
    RemoteRepositoryOut,
    RepositoryCreate,
    RepositoryOut,
    RepositoryUpdate,
)
from app.security.authorization import Principal
from app.security.input_sanitization import validate_branch_name, validate_full_name, validate_slug
from app.services import audit_service

router = APIRouter(prefix="/repositories", tags=["repositories"])


def _get(session: Session, repository_id: int) -> Repository:
    repo = session.get(Repository, repository_id)
    if repo is None:
        raise NotFoundError(f"Repository {repository_id} not found")
    return repo


@router.get("", response_model=Page[RepositoryOut])
def list_repositories(page: Pagination = Depends(), session: Session = Depends(db_dep),
                      _: Principal = Depends(current_principal)) -> Page[RepositoryOut]:
    total = session.scalar(select(func.count(Repository.id))) or 0
    rows = session.scalars(select(Repository).order_by(Repository.is_demo, Repository.full_name)
                           .limit(page.limit).offset(page.offset))
    return Page(items=[repository_out(r) for r in rows], total=total, limit=page.limit, offset=page.offset)


@router.get("/remote", response_model=list[RemoteRepositoryOut])
async def list_remote(session: Session = Depends(db_dep), factory: ProviderFactory = Depends(factory_dep),
                      _: Principal = Depends(current_principal)) -> list[RemoteRepositoryOut]:
    client = factory.github(session)
    try:
        repos = await client.list_repositories()
    finally:
        await client.aclose()
    connected = {r.full_name: r.id for r in session.scalars(select(Repository).where(Repository.provider == "github"))}
    return [RemoteRepositoryOut(full_name=r.full_name, default_branch=r.default_branch, private=r.private,
                                connected_id=connected.get(r.full_name)) for r in repos]


@router.post("", response_model=RepositoryOut, status_code=201)
async def connect_repository(body: RepositoryCreate, session: Session = Depends(db_dep),
                             factory: ProviderFactory = Depends(factory_dep),
                             principal: Principal = Depends(admin)) -> RepositoryOut:
    validate_full_name(body.full_name)
    client = factory.github(session)
    try:
        info = await client.get_repository(body.full_name)
    finally:
        await client.aclose()
    existing = session.scalar(select(Repository).where(Repository.provider == "github",
                                                       Repository.provider_repository_id
                                                       == info.provider_repository_id))
    if existing is not None:
        raise ConflictError(f"{info.full_name} is already connected (id {existing.id}).")
    repo = Repository(provider="github", provider_repository_id=info.provider_repository_id, owner=info.owner,
                      name=info.name, full_name=info.full_name, default_branch=info.default_branch,
                      private=info.private, enabled=body.enabled,
                      installation_id=factory.github_installation_id(session),
                      last_synced_at=utcnow())
    session.add(repo)
    session.flush()
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="repository.connected",
                         resource_type="repository", resource_id=repo.id,
                         details={"full_name": repo.full_name, "enabled": repo.enabled})
    session.refresh(repo)
    return repository_out(repo)


@router.get("/{repository_id}", response_model=RepositoryOut)
def get_repository(repository_id: int, session: Session = Depends(db_dep),
                   _: Principal = Depends(current_principal)) -> RepositoryOut:
    return repository_out(_get(session, repository_id))


@router.patch("/{repository_id}", response_model=RepositoryOut)
def update_repository(repository_id: int, body: RepositoryUpdate, session: Session = Depends(db_dep),
                      principal: Principal = Depends(admin)) -> RepositoryOut:
    repo = _get(session, repository_id)
    changes: dict[str, object] = {}
    if body.enabled is not None and body.enabled != repo.enabled:
        repo.enabled = body.enabled
        changes["enabled"] = body.enabled
    if body.test_commands is not None:
        if repo.is_demo:
            raise InvalidRequestError("Demo repository commands cannot be changed.")
        repo.test_commands_json = [c.model_dump() for c in body.test_commands] or None
        changes["test_commands"] = repo.test_commands_json
    if changes:
        audit_service.record(session, actor_type="user", actor_id=principal.external_id,
                             action="repository.updated", resource_type="repository", resource_id=repo.id,
                             details=changes)
    session.flush()
    session.refresh(repo)
    return repository_out(repo)


@router.post("/{repository_id}/test-connection", response_model=ConnectionTestResult)
async def test_repository(repository_id: int, session: Session = Depends(db_dep),
                          factory: ProviderFactory = Depends(factory_dep),
                          _: Principal = Depends(current_principal)) -> ConnectionTestResult:
    repo = _get(session, repository_id)
    if repo.is_demo:
        return ConnectionTestResult(provider="demo", ok=True, message="Demo repository (local fixture).")
    client = factory.github(session, repo)
    try:
        info = await client.get_repository(repo.full_name)
        head = await client.get_branch_head(repo.full_name, info.default_branch)
    except Exception as exc:
        return ConnectionTestResult(provider="github", ok=False, message=str(getattr(exc, "message", exc))[:300])
    finally:
        await client.aclose()
    perms = info.permissions
    return ConnectionTestResult(provider="github", ok=True,
                                message=f"Accessible. Default branch {info.default_branch} at {(head or '')[:12]}.",
                                details={"permissions": perms, "private": info.private})


@router.post("/{repository_id}/sync", response_model=RepositoryOut)
async def sync_repository(repository_id: int, session: Session = Depends(db_dep),
                          factory: ProviderFactory = Depends(factory_dep),
                          principal: Principal = Depends(admin)) -> RepositoryOut:
    repo = _get(session, repository_id)
    if not repo.is_demo:
        client = factory.github(session, repo)
        try:
            info = await client.get_repository(repo.full_name)
        finally:
            await client.aclose()
        if info.provider_repository_id != repo.provider_repository_id:
            raise ConflictError("The repository at this name has a different id (renamed or recreated).")
        repo.default_branch = info.default_branch
        repo.private = info.private
        repo.owner, repo.name, repo.full_name = info.owner, info.name, info.full_name
    repo.last_synced_at = utcnow()
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="repository.synced",
                         resource_type="repository", resource_id=repo.id)
    session.flush()
    session.refresh(repo)
    return repository_out(repo)


@router.post("/{repository_id}/mappings", response_model=MappingOut, status_code=201)
def add_mapping(repository_id: int, body: MappingIn, session: Session = Depends(db_dep),
                principal: Principal = Depends(admin)) -> MappingOut:
    repo = _get(session, repository_id)
    validate_slug(body.sentry_organization, "Sentry organization")
    validate_slug(body.sentry_project, "Sentry project")
    if body.branch:
        validate_branch_name(body.branch)
    existing = session.scalar(select(RepositoryMapping).where(
        RepositoryMapping.sentry_organization == body.sentry_organization,
        RepositoryMapping.sentry_project == body.sentry_project))
    if existing is not None:
        raise ConflictError("This Sentry project is already mapped; delete the existing mapping first.")
    mapping = RepositoryMapping(sentry_organization=body.sentry_organization, sentry_project=body.sentry_project,
                                repository_id=repo.id, branch=body.branch, path_prefixes_json=body.path_prefixes,
                                strip_prefixes_json=body.strip_prefixes, is_demo=repo.is_demo)
    session.add(mapping)
    session.flush()
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="mapping.created",
                         resource_type="repository", resource_id=repo.id, details=body.model_dump())
    return mapping_out(mapping)


@router.delete("/mappings/{mapping_id}", status_code=204)
def delete_mapping(mapping_id: int, session: Session = Depends(db_dep), principal: Principal = Depends(admin)) -> None:
    mapping = session.get(RepositoryMapping, mapping_id)
    if mapping is None:
        raise NotFoundError(f"Mapping {mapping_id} not found")
    audit_service.record(session, actor_type="user", actor_id=principal.external_id, action="mapping.deleted",
                         resource_type="repository", resource_id=mapping.repository_id,
                         details={"sentry": f"{mapping.sentry_organization}/{mapping.sentry_project}"})
    session.delete(mapping)
