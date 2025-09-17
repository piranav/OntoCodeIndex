"""Typed messages for LSP communication (placeholder)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Position:
    line: int
    character: int


@dataclass(slots=True)
class Range:
    start: Position
    end: Position


__all__ = ["Position", "Range"]
