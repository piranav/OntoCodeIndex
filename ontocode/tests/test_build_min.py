"""End-to-end tests for the minimal Next.js fixture."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH
from typer.testing import CliRunner

from ontocode.cli import app
from ontocode.paths import encode_path_for_graph

LACO = Namespace("https://example.org/laco#")
NEXT = Namespace("https://example.org/next#")

SAMPLE_PROJECT = Path(__file__).resolve().parent / "data" / "next_min"


@pytest.fixture()
def build_output(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "sample_repo"
    shutil.copytree(SAMPLE_PROJECT, repo)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "build",
            "--repo",
            str(repo),
            "--commit",
            "testsha",
        ],
    )
    assert result.exit_code == 0, f"CLI failed: {result.stdout}"
    commit_dir = repo / ".ontology" / "commit" / "testsha"
    return {
        "repo": repo,
        "commit_dir": commit_dir,
        "facts": commit_dir / "facts" / "files",
        "inferred": commit_dir / "inferred" / "merged.ttl",
        "report": commit_dir / "reports" / "shacl_report.ttl",
    }


def _load_graph(path: Path) -> Graph:
    graph = Graph()
    graph.parse(path)
    return graph


def test_facts_written(build_output: dict[str, Path]) -> None:
    repo = build_output["repo"]
    facts_dir = build_output["facts"]
    file_name = f"{encode_path_for_graph(Path('app/home/page.tsx'))}.ttl"
    file_graph_path = facts_dir / file_name
    assert file_graph_path.exists(), "Expected page.tsx facts to be emitted"
    graph = _load_graph(file_graph_path)
    repo_name = repo.name
    file_iri = URIRef(
        f"laco://repo/{repo_name}/commit/testsha/file/{encode_path_for_graph(Path('app/home/page.tsx'))}"
    )
    default_unit = None
    for unit in graph.subjects(RDF.type, LACO.Callable):
        if (unit, LACO.isExportedDefault, Literal(True)) in graph:
            default_unit = unit
            break
    assert default_unit is not None, "Default export callable not found"
    assert (file_iri, LACO.defines, default_unit) in graph
    calls = list(graph.triples((default_unit, LACO.calls, None)))
    assert calls, "Expected at least one call occurrence from default export"


def test_route_pattern(build_output: dict[str, Path]) -> None:
    inferred_path = build_output["inferred"]
    inferred_graph = _load_graph(inferred_path)
    patterns = list(inferred_graph.objects(None, NEXT.routePattern))
    assert Literal("/home") in patterns


def test_shacl_valid(build_output: dict[str, Path]) -> None:
    report = _load_graph(build_output["report"])
    severities = set(report.objects(None, SH.resultSeverity))
    assert SH.Violation not in severities


def test_uses_client_directive(build_output: dict[str, Path]) -> None:
    inferred_graph = _load_graph(build_output["inferred"])
    uses_client = list(inferred_graph.triples((None, NEXT.usesClient, Literal(True))))
    assert uses_client, "Expected usesClient inference for 'use client' directive"
