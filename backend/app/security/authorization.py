"""Authorization decisions.

All permission checks live in code. Nothing produced by the model, a repository, a Sentry
event or test output can change these decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import UserRole
from app.core.exceptions import PermissionDenied, PolicyViolation
from app.db.models import Repository

ROLE_RANK = {UserRole.VIEWER: 0, UserRole.REVIEWER: 1, UserRole.ADMIN: 2}


@dataclass(frozen=True)
class Principal:
    user_id: int
    external_id: str
    display_name: str
    role: UserRole
    actor_type: str = "user"

    def has_role(self, minimum: UserRole) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]


def require_role(principal: Principal, minimum: UserRole) -> None:
    if principal.actor_type != "user":
        raise PermissionDenied("Only authenticated human users may perform this action.")
    if not principal.has_role(minimum):
        raise PermissionDenied(f"This action requires the '{minimum.value}' role.")


def can_approve(principal: Principal) -> bool:
    return principal.actor_type == "user" and principal.has_role(UserRole.REVIEWER)


def ensure_repository_allowed(repository: Repository | None, *, write: bool = False) -> Repository:
    """The repository allowlist: only enabled repositories may be read or written."""
    if repository is None:
        raise PolicyViolation("No repository is mapped for this issue.")
    if not repository.enabled:
        raise PolicyViolation(
            f"Repository {repository.full_name} is not on the allowlist (enable it on the Repositories page)."
        )
    if write and repository.is_demo:
        raise PolicyViolation("Demo repositories cannot be written to.")
    return repository
