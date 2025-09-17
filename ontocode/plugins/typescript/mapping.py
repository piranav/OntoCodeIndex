"""Map TypeScript extractor JSON facts into RDF triples."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import quote

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from ...paths import encode_path_for_graph

LACO = Namespace("https://example.org/laco#")
LASA = Namespace("https://example.org/lasa#")
NEXT = Namespace("https://example.org/next#")
DCT = Namespace("http://purl.org/dc/terms/")
PROV = Namespace("http://www.w3.org/ns/prov#")
TS_EXT = Namespace("https://example.org/laco/ts#")

RELATION_TO_PREDICATE = {
    "calls": LACO.calls,
    "references": LACO.references,
    "reads": LACO.readsFrom,
    "writes": LACO.writesTo,
}

UNIT_KIND_TO_TYPE = {
    "callable": LACO.Callable,
    "classifier": LACO.Classifier,
    "variable": LACO.Variable,
    "parameter": LACO.Parameter,
    "type": LACO.Type,
}


@dataclass(slots=True)
class SymbolIndex:
    """Global index of symbol identifiers resolved from extractor payloads."""

    repo_name: str
    commit_sha: str
    by_symbol: dict[str, URIRef] = field(default_factory=dict)
    by_qname: dict[str, URIRef] = field(default_factory=dict)
    dangling: dict[str, URIRef] = field(default_factory=dict)
    dangling_emitted: set[str] = field(default_factory=set)

    def register_payload(self, payload: dict[str, Any]) -> None:
        units = payload.get("units", [])
        for unit in units:
            symbol_id = unit.get("symbolId")
            qualified_name = unit.get("qualifiedName")
            if not symbol_id or not qualified_name:
                continue
            uri = self.make_unit_uri(symbol_id)
            self.by_symbol[symbol_id] = uri
            self.by_qname.setdefault(str(qualified_name), uri)

    def make_unit_uri(self, symbol_id: str) -> URIRef:
        return URIRef(f"laco://sym/{self.repo_name}/{self.commit_sha}/{symbol_id}")

    def for_symbol(self, symbol_id: str | None) -> URIRef | None:
        if symbol_id is None:
            return None
        return self.by_symbol.get(symbol_id)

    def for_qualified_name(self, qualified_name: str | None) -> URIRef | None:
        if not qualified_name:
            return None
        return self.by_qname.get(qualified_name)

    def ensure_dangling(self, qualified_name: str) -> URIRef:
        if qualified_name in self.dangling:
            return self.dangling[qualified_name]
        encoded = quote(qualified_name, safe="")
        uri = URIRef(
            f"laco://sym/{self.repo_name}/{self.commit_sha}/dangling/{encoded}"
        )
        self.dangling[qualified_name] = uri
        self.by_qname.setdefault(qualified_name, uri)
        return uri


@dataclass(slots=True)
class MappingContext:
    repo_name: str
    repo_path: Path
    commit_sha: str
    commit_iri: URIRef
    repo_iri: URIRef
    symbol_index: SymbolIndex

    def file_iri(self, relative_path: str) -> URIRef:
        encoded = encode_path_for_graph(Path(relative_path))
        return URIRef(
            f"laco://repo/{self.repo_name}/commit/{self.commit_sha}/file/{encoded}"
        )


def initialise_graph() -> Graph:
    graph = Graph()
    graph.bind("laco", LACO)
    graph.bind("lasa", LASA)
    graph.bind("next", NEXT)
    graph.bind("dct", DCT)
    graph.bind("prov", PROV)
    graph.bind("ts", TS_EXT)
    return graph


def add_span(graph: Graph, parent: URIRef | BNode, span: dict[str, Any]) -> None:
    span_node = BNode()
    graph.add((parent, LACO.span, span_node))
    for field_name in ("startLine", "endLine", "startCol", "endCol"):
        if field_name in span:
            predicate = getattr(LACO, field_name)
            graph.add((span_node, predicate, Literal(int(span[field_name]), datatype=XSD.integer)))


def apply_mapping(payload: dict[str, Any], context: MappingContext) -> Graph:
    graph = initialise_graph()
    relative_path = str(payload["filePath"])
    file_iri = context.file_iri(relative_path)
    graph.add((file_iri, RDF.type, LACO.SourceFile))
    graph.add((file_iri, DCT.path, Literal(relative_path)))
    graph.add((file_iri, LACO.sha256, Literal(str(payload.get("sha256")))))
    graph.add((file_iri, LACO.atCommit, context.commit_iri))

    if payload.get("hasUseClientDirective") is True:
        graph.add((file_iri, TS_EXT.hasUseClientDirective, Literal(True)))

    for unit in payload.get("units", []):
        unit_symbol = unit.get("symbolId")
        qualified_name = unit.get("qualifiedName")
        if not unit_symbol or not qualified_name:
            continue
        unit_uri = context.symbol_index.make_unit_uri(str(unit_symbol))
        unit_type = UNIT_KIND_TO_TYPE.get(str(unit.get("kind")), LACO.Unit)
        graph.add((unit_uri, RDF.type, unit_type))
        graph.add((unit_uri, LACO.declaredIn, file_iri))
        graph.add((unit_uri, LACO.qualifiedName, Literal(str(qualified_name))))
        graph.add((unit_uri, LACO.symbolId, Literal(str(unit_symbol))))
        graph.add((unit_uri, LACO.atCommit, context.commit_iri))
        ast_path = unit.get("astPath")
        if ast_path:
            graph.add((unit_uri, LACO.astPath, Literal(str(ast_path))))
        if "span" in unit and isinstance(unit["span"], dict):
            add_span(graph, unit_uri, unit["span"])
        if unit.get("isExportedDefault"):
            graph.add((unit_uri, LACO.isExportedDefault, Literal(True)))
        if unit.get("isAsync"):
            graph.add((unit_uri, URIRef("https://example.org/laco/ts#isAsync"), Literal(True)))
        graph.add((file_iri, LACO.defines, unit_uri))

    for import_record in payload.get("imports", []):
        from_value = import_record.get("from")
        resolved_kind = import_record.get("resolvedKind")
        resolved = import_record.get("resolved")
        if not from_value:
            continue
        if resolved_kind == "package" and isinstance(from_value, str):
            pkg_uri = URIRef(f"laco://pkg/{quote(from_value, safe='')}")
            graph.add((pkg_uri, RDF.type, LACO.Package))
            graph.add((pkg_uri, DCT.title, Literal(from_value)))
            graph.add((file_iri, LACO.importsFrom, pkg_uri))
        elif resolved_kind == "file" and isinstance(resolved, str):
            target_iri = context.file_iri(resolved)
            graph.add((file_iri, LACO.importsFrom, target_iri))
        else:
            target_uri = URIRef(f"laco://ext/{quote(str(from_value), safe='')}")
            graph.add((file_iri, LACO.importsFrom, target_uri))

    for export_record in payload.get("exports", []):
        unit_symbol = export_record.get("unitSymbolId")
        if not unit_symbol:
            continue
        unit_uri = context.symbol_index.for_symbol(str(unit_symbol))
        if unit_uri:
            graph.add((file_iri, LACO.exports, unit_uri))

    for occurrence in payload.get("occurrences", []):
        subject_uri = context.symbol_index.for_symbol(occurrence.get("subjectSymbolId"))
        if not subject_uri:
            continue
        object_uri = context.symbol_index.for_symbol(occurrence.get("objectSymbolId"))
        created_dangling = False
        qname_value = occurrence.get("objectQName")
        if not object_uri and qname_value:
            object_uri = context.symbol_index.for_qualified_name(str(qname_value))
            if not object_uri:
                object_uri = context.symbol_index.ensure_dangling(str(qname_value))
                created_dangling = True
        if not object_uri:
            continue
        if created_dangling and isinstance(qname_value, str) and qname_value not in context.symbol_index.dangling_emitted:
            graph.add((object_uri, RDF.type, LACO.Unit))
            graph.add((object_uri, LACO.qualifiedName, Literal(qname_value)))
            context.symbol_index.dangling_emitted.add(qname_value)
        relation = str(occurrence.get("relation") or "calls")
        predicate = RELATION_TO_PREDICATE.get(relation)
        if predicate:
            graph.add((subject_uri, predicate, object_uri))
        occ_node = BNode()
        graph.add((occ_node, RDF.type, LACO.Occurrence))
        graph.add((occ_node, LACO.ofRelation, Literal(relation)))
        graph.add((occ_node, LACO.subject, subject_uri))
        graph.add((occ_node, LACO.object, object_uri))
        graph.add((occ_node, LACO.inFile, file_iri))
        if isinstance(occurrence.get("span"), dict):
            add_span(graph, occ_node, occurrence["span"])

    return graph


__all__ = ["MappingContext", "SymbolIndex", "apply_mapping", "LACO", "NEXT", "TS_EXT"]
