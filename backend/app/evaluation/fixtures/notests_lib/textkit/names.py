"""Name formatting."""


def initials(full_name: str) -> str:
    parts = full_name.split()
    return (parts[0][0] + parts[1][0]).upper()
