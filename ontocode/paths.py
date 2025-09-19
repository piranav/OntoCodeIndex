"""Path helper utilities."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


def _normalize_relative(path: str) -> str:
    """Return a forward-slashed relative path without leading separators."""
    return path.replace("\\", "/").lstrip("/")


def ensure_out_dir(base: Path, commit: str) -> Path:
    """Return commit output directory under base path."""
    commit_dir = base / "commit" / commit
    commit_dir.mkdir(parents=True, exist_ok=True)
    return commit_dir


def encode_path_for_graph(path: Path) -> str:
    """Encode relative path for use in IRI."""
    return quote(str(path).replace("\\", "/"), safe="")


def flatten_relative_path(path: str | Path) -> str:
    """Flatten a relative path into a single filename-safe token."""
    text = str(path)
    normalized = _normalize_relative(text)
    if not normalized:
        return "_"
    parts = [segment for segment in normalized.split("/") if segment]
    flattened = "__".join(parts)
    return flattened


__all__ = ["ensure_out_dir", "encode_path_for_graph", "flatten_relative_path"]
