"""End-to-end tests for the minimal Next.js fixture."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH
from typer.testing import CliRunner

from ontocode.cli import app
from ontocode.paths import encode_path_for_graph, flatten_relative_path

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
        "mount": commit_dir / "mount.json",
        "meta": commit_dir / "ontology_meta.json",
    }


def _load_graph(path: Path) -> Graph:
    graph = Graph()
    graph.parse(path)
    return graph


def _load_union_graph(facts_dir: Path, inferred_path: Path) -> Graph:
    graph = Graph()
    for ttl_file in facts_dir.glob("*.ttl"):
        graph.parse(ttl_file)
    if inferred_path.exists():
        graph.parse(inferred_path)
    return graph


@pytest.fixture()
def union_graph(build_output: dict[str, Path]) -> Graph:
    return _load_union_graph(build_output["facts"], build_output["inferred"])


def test_facts_written(build_output: dict[str, Path]) -> None:
    repo = build_output["repo"]
    facts_dir = build_output["facts"]
    file_graph_path = facts_dir / Path(f"{flatten_relative_path('app/home/page.tsx')}.ttl")
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
    assert Literal("/api/hello") in patterns


def test_shacl_valid(build_output: dict[str, Path]) -> None:
    report = _load_graph(build_output["report"])
    severities = set(report.objects(None, SH.resultSeverity))
    assert SH.Violation not in severities


def test_uses_client_directive(build_output: dict[str, Path]) -> None:
    inferred_graph = _load_graph(build_output["inferred"])
    uses_client = list(inferred_graph.triples((None, NEXT.usesClient, Literal(True))))
    assert uses_client, "Expected usesClient inference for 'use client' directive"


def test_mount_artifact(build_output: dict[str, Path], union_graph: Graph) -> None:
    mount_path = build_output["mount"]
    assert mount_path.exists(), "Expected mount.json to be emitted"
    payload = json.loads(mount_path.read_text(encoding="utf-8"))
    assert payload["union_default_graph"] is True
    assert payload["facts_dir"].endswith("/facts/files")
    prefixes = payload.get("prefixes", {})
    assert "laco" in prefixes and prefixes["laco"].endswith("laco#")
    facts_dir = build_output["facts"]
    sum_triples = 0
    for entry in payload["graph_index"]:
        ttl_name = entry["ttl_file"]
        ttl_path = facts_dir / ttl_name
        assert ttl_path.exists(), f"Missing shard {ttl_name} listed in mount"
        sum_triples += int(entry["triples"])
    repo = build_output["repo"]
    for vocab_relative in payload["vocab_files"]:
        assert (repo / vocab_relative).exists(), f"Missing vocab file {vocab_relative}"
        assert "/commit/" in vocab_relative, "Expected vocab to be scoped per commit"
    facts_graph = Graph()
    for ttl_file in facts_dir.glob("*.ttl"):
        facts_graph.parse(ttl_file)
    facts_query = facts_graph.query("SELECT (COUNT(?s) AS ?count) WHERE { ?s ?p ?o }")
    facts_row = next(iter(facts_query), None)
    assert facts_row is not None
    facts_total = int(facts_row[0].toPython())
    assert sum_triples == facts_total
    total_query = union_graph.query("SELECT (COUNT(?s) AS ?count) WHERE { ?s ?p ?o }")
    total_row = next(iter(total_query), None)
    assert total_row is not None
    total_count = int(total_row[0].toPython())
    assert total_count >= sum_triples


def _count_instances(graph: Graph, cls_iri: str) -> int:
    query = f"SELECT (COUNT(?s) AS ?count) WHERE {{ ?s a <{cls_iri}> }}"
    result = graph.query(query)
    row = next(iter(result), None)
    return int(row[0].toPython()) if row else 0


def test_meta_content(build_output: dict[str, Path], union_graph: Graph) -> None:
    meta_path = build_output["meta"]
    assert meta_path.exists(), "Expected ontology_meta.json to be emitted"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    total_query = union_graph.query("SELECT (COUNT(?s) AS ?count) WHERE { ?s ?p ?o }")
    total_row = next(iter(total_query), None)
    assert total_row is not None
    total_count = int(total_row[0].toPython())
    classes = {entry["id"]: int(entry["count"]) for entry in payload["tbox"]["classes"]}
    assert "laco:Callable" in classes
    assert "laco:SourceFile" in classes
    assert classes["laco:Callable"] == _count_instances(union_graph, "https://example.org/laco#Callable")
    assert classes["laco:SourceFile"] == _count_instances(union_graph, "https://example.org/laco#SourceFile")
    object_ids = {entry["id"] for entry in payload["tbox"]["object_properties"]}
    assert {"laco:declaredIn", "laco:calls"}.issubset(object_ids)
    data_ids = {entry["id"] for entry in payload["tbox"]["data_properties"]}
    assert {"laco:qualifiedName", "dct:path"}.issubset(data_ids)
    versions = payload["vocabulary_versions"]
    for key in ("laco.ttl_sha256", "lasa.ttl_sha256"):
        assert key in versions and len(versions[key]) == 64
    rule_packs = payload.get("rbox", {}).get("rule_packs", [])
    assert rule_packs, "Expected rule pack timestamps"
    histograms = payload.get("histograms", {})
    assert {"capabilities", "qualities", "roles"} <= histograms.keys()
    for values in histograms.values():
        assert isinstance(values, list)
    stats = payload.get("stats", {})
    assert stats.get("union_triples") == total_count
    shacl_info = payload.get("shacl")
    assert shacl_info and "conforms" in shacl_info
    assert shacl_info["report_file"].endswith("/reports/shacl_report.ttl")
    assert payload.get("ontocode_version")
    assert payload.get("rules_core_sha")
    assert payload.get("rules_next_sha")


def test_disable_mount_meta(tmp_path: Path) -> None:
    repo = tmp_path / "sample_repo_no_meta"
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
            "--no-emit-mount",
            "--no-emit-meta",
        ],
    )
    assert result.exit_code == 0, f"CLI failed: {result.stdout}"
    commit_dir = repo / ".ontology" / "commit" / "testsha"
    assert not (commit_dir / "mount.json").exists()
    assert not (commit_dir / "ontology_meta.json").exists()
