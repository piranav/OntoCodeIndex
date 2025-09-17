"""RDF writer helpers."""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph


def write_graph(graph: Graph, file_path: Path) -> None:
    """Serialize graph to Turtle at file path."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(destination=file_path, format="turtle")


__all__ = ["write_graph"]
