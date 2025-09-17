"""Git helper functions."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .logging import get_logger

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class GitInfo:
    repo_root: str
    head_commit: str


def git_rev_parse(repo: str) -> str:
    """Return the HEAD commit SHA for a repository."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - system dependent
        LOGGER.error("git rev-parse failed: %s", exc)
        raise
    return result.stdout.strip()


__all__ = ["GitInfo", "git_rev_parse"]
