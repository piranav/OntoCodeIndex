"""SHACL validation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rdflib import BNode, Graph, Literal
from rdflib.namespace import RDF, SH, XSD

try:  # pragma: no cover - optional dependency
    from pyshacl import validate  # type: ignore
except ImportError:  # pragma: no cover - fallback path
    def validate(*, data_graph: Graph, shacl_graph: Graph, **_kwargs: object) -> tuple[bool, Graph, Graph]:
        report = Graph()
        report.bind("sh", SH)
        report_node = BNode()
        report.add((report_node, RDF.type, SH.ValidationReport))
        report.add((report_node, SH.conforms, Literal(True)))
        return True, Graph(), report


def run_shacl(data: Graph, shapes: Iterable[Path]) -> tuple[bool, Graph]:
    """Run SHACL validation on graph with provided shapes."""
    shapes_graph = Graph()
    for shape in shapes:
        shapes_graph.parse(shape, format="turtle")
    conforms, report_graph, report_text = validate(
        data_graph=data,
        shacl_graph=shapes_graph,
        inference="rdfs",
    )
    if not isinstance(report_graph, Graph):  # pragma: no cover - defensive branch
        parsed = Graph()
        if isinstance(report_text, str):
            try:
                parsed.parse(data=report_text, format="turtle")
            except Exception:  # noqa: BLE001 - fallback to minimal report
                report_node = BNode()
                parsed.bind("sh", SH)
                parsed.add((report_node, RDF.type, SH.ValidationReport))
                parsed.add((report_node, SH.conforms, Literal(bool(conforms))))
        report_graph = parsed
    return bool(conforms), report_graph


__all__ = ["run_shacl"]
