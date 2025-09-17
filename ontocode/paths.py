"""Path helper utilities."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


def ensure_out_dir(base: Path, commit: str) -> Path:
    """Return commit output directory under base path."""
    commit_dir = base / "commit" / commit
    commit_dir.mkdir(parents=True, exist_ok=True)
    return commit_dir


def encode_path_for_graph(path: Path) -> str:
    """Encode relative path for use in IRI."""
    return quote(str(path).replace("\\", "/"), safe="")


__all__ = ["ensure_out_dir", "encode_path_for_graph"]
