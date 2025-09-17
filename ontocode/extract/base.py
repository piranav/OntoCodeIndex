"""Extraction interfaces for language plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Protocol


class ExtractedFact(Protocol):
    """Protocol for extracted per-file payloads."""

    file_path: Path


class LanguageExtractor(ABC):
    """Interface for language-specific extractors."""

    @abstractmethod
    def supported_extensions(self) -> Iterable[str]:
        """Return supported file extensions."""

    @abstractmethod
    def extract(self, files: Iterable[Path]) -> Iterable[ExtractedFact]:
        """Yield extracted facts for provided files."""


__all__ = ["LanguageExtractor", "ExtractedFact"]
