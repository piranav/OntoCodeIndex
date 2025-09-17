"""Run SPARQL CONSTRUCT rule packs."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rdflib import Graph


def _split_constructs(body_text: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    for line in body_text.splitlines():
        current.append(line)
        if line.strip().endswith(";"):
            snippet = "\n".join(current).rstrip(";").strip()
            if snippet:
                segments.append(snippet)
            current = []
    remainder = "\n".join(current).strip()
    if remainder:
        segments.append(remainder)
    return segments


def run_rule_packs(facts: Graph, rule_files: Iterable[Path]) -> Graph:
    """Execute a sequence of SPARQL CONSTRUCT rule files and return inferred triples."""
    working = Graph()
    working += facts
    inferred_total = Graph()
    for rule_path in rule_files:
        text = rule_path.read_text(encoding="utf-8")
        prefix_lines: list[str] = []
        body_lines: list[str] = []
        for line in text.splitlines():
            if line.strip().upper().startswith("PREFIX"):
                prefix_lines.append(line)
            else:
                body_lines.append(line)
        for query_body in _split_constructs("\n".join(body_lines)):
            if not query_body:
                continue
            query_text = "\n".join(prefix_lines + [query_body])
            result = working.query(query_text)
            result_graph = getattr(result, "graph", None)
            if result_graph is None:
                temp = Graph()
                for triple in result:
                    temp.add(triple)
                result_graph = temp
            inferred_total += result_graph
            working += result_graph
    return inferred_total


__all__ = ["run_rule_packs"]
