"""Simple JSON-RPC client wrapper for typescript-language-server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class LspClient:
    """Placeholder LSP client."""

    root: Path

    def references(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return []

    def shutdown(self) -> None:
        """Placeholder shutdown."""
        return None


__all__ = ["LspClient"]
