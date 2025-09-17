"""Logging utilities."""

from __future__ import annotations

import logging
from typing import Iterable

from rich.console import Console
from rich.logging import RichHandler


def configure_logging(level: str = "INFO", *, rich_tracebacks: bool = True) -> None:
    """Configure root logger for the CLI."""
    console = Console(stderr=True)
    handler = RichHandler(console=console, rich_tracebacks=rich_tracebacks, markup=False)
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str, *extra_names: Iterable[str]) -> logging.Logger:
    """Return a namespaced logger."""
    namespace = ".".join([name, *extra_names]) if extra_names else name
    return logging.getLogger(namespace)


__all__ = ["configure_logging", "get_logger"]
