"""Privacy-safe repository provenance for browser evidence bundles."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TypedDict


_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RepositoryProvenance(TypedDict):
    commit: str | None
    dirty: bool | None


def _git(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPOSITORY_ROOT), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def get_repository_provenance() -> RepositoryProvenance:
    """Discover commit and clean-tree state; never trust declarations."""

    discovered = (_git("rev-parse", "HEAD") or "").lower()
    commit = discovered if _COMMIT_PATTERN.fullmatch(discovered) else None
    status = _git("status", "--porcelain", "--untracked-files=normal")
    dirty = None if status is None else bool(status)

    return {"commit": commit, "dirty": dirty}
