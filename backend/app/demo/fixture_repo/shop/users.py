"""User presentation helpers."""

from dataclasses import dataclass


@dataclass
class Profile:
    nickname: str | None = None


@dataclass
class User:
    id: int
    email: str
    profile: Profile | None = None


def display_name(user: User) -> str:
    """Name shown in the storefront header."""
    nickname = user.profile.nickname
    return nickname or user.email.split("@")[0]
