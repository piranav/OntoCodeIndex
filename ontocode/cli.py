"""Command-line interface for OntoCodeIndex."""

from __future__ import annotations

import fnmatch
import logging
import re
import shutil
from pathlib import Path
from typing import Iterable, Optional

import typer
import yaml
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

from .config import LspConfig, OntoCodeConfig
from .extract.ts_bridge import TsExtractorRunner
from .git_utils import git_rev_parse
from .logging import configure_logging, get_logger
from .paths import ensure_out_dir, encode_path_for_graph
from .plugins.typescript.mapping import LACO, NEXT, TS_EXT, MappingContext, SymbolIndex, apply_mapping
from .rdf.rules_runner import run_rule_packs
from .rdf.shacl_runner import run_shacl as execute_shacl
from .rdf.writer import write_graph

app = typer.Typer(help="Extract LACO/LASA ontologies from codebases.")
LOGGER = get_logger(__name__)



@app.callback()
def main() -> None:
    """OntoCodeIndex CLI root."""
    return None

LANGUAGE_GLOBS: dict[str, tuple[str, ...]] = {
    "ts": ("**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"),
}

PACKAGE_ROOT = Path(__file__).resolve().parent
VOCAB_ROOT = PACKAGE_ROOT / "rdf" / "vocab"
SHAPES_ROOT = PACKAGE_ROOT / "rdf" / "shapes"
RULES_ROOT = PACKAGE_ROOT / "rdf" / "rules"
DCT_PATH = URIRef("http://purl.org/dc/terms/path")


def _load_yaml_config(repo: Path) -> dict[str, object]:
    config_path = repo / "ontocode.yaml"
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as error:  # pragma: no cover - yaml error path
        raise typer.BadParameter(f"Failed to parse {config_path}: {error}") from error
    if not isinstance(data, dict):
        raise typer.BadParameter("ontocode.yaml must contain a mapping")
    return data


def _merge_config(repo: Path, cli_options: dict[str, object]) -> OntoCodeConfig:
    file_overrides = _load_yaml_config(repo)
    merged: dict[str, object] = {**file_overrides, **cli_options}
    if "langs" in merged and isinstance(merged["langs"], str):
        merged["langs"] = [part.strip() for part in str(merged["langs"]).split(",") if part.strip()]
    ignore_patterns = merged.get("ignore")
    if ignore_patterns is None:
        merged["ignore"] = []
    return OntoCodeConfig(**merged)


def _collect_matchers(ignore: Iterable[str]) -> list[str]:
    patterns: list[str] = []
    for pattern in ignore:
        if pattern:
            patterns.append(pattern)
    return patterns


def _is_ignored(relative_path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def _collect_language_files(repo: Path, languages: Iterable[str], ignore_patterns: list[str]) -> list[Path]:
    selected: dict[Path, None] = {}
    for language in languages:
        globs = LANGUAGE_GLOBS.get(language)
        if not globs:
            LOGGER.warning("Unsupported language '%s' requested; skipping", language)
            continue
        for pattern in globs:
            for path in repo.glob(pattern):
                if not path.is_file():
                    continue
                rel = path.relative_to(repo).as_posix()
                if _is_ignored(rel, ignore_patterns):
                    continue
                selected[path.resolve()] = None
    return sorted(selected)


def _derive_route_pattern(relative_path: str) -> str:
    try:
        sub_path = relative_path.split("app/", 1)[1]
    except IndexError:
        return f"/{relative_path.strip('/')}"
    cleaned = re.sub(r"\.(tsx|jsx|ts|js)$", "", sub_path)
    segments = []
    for segment in cleaned.split("/"):
        if segment in {"page", "layout", "route"}:
            continue
        if not segment:
            continue
        if segment.startswith("[[...") and segment.endswith("]]"):
            segments.append("*" + segment[4:-2])
        elif segment.startswith("[...") and segment.endswith("]"):
            segments.append("*" + segment[4:-1])
        elif segment.startswith("[") and segment.endswith("]"):
            segments.append(":" + segment[1:-1])
        else:
            segments.append(segment)
    return "/" + "/".join(segments).replace("//", "/") if segments else "/"


def _python_inference(facts: Graph) -> Graph:
    inferred = Graph()
    inferred.bind("next", NEXT)
    inferred.bind("laco", LACO)
    inferred.bind("ts", TS_EXT)
    for file_iri in facts.subjects(RDF.type, LACO.SourceFile):
        path_literal = facts.value(file_iri, DCT_PATH)
        if path_literal is None:
            continue
        relative_path = str(path_literal)
        default_units = [
            unit
            for unit in facts.objects(file_iri, LACO.defines)
            if (unit, LACO.isExportedDefault, Literal(True)) in facts
        ]
        if relative_path.startswith("app/"):
            if relative_path.endswith("page.tsx") or relative_path.endswith("page.jsx"):
                for unit in default_units:
                    inferred.add((unit, RDF.type, NEXT.Page))
                    inferred.add((unit, NEXT.segmentType, Literal("page")))
                    inferred.add((unit, NEXT.routePattern, Literal(_derive_route_pattern(relative_path))))
                    if (file_iri, TS_EXT.hasUseClientDirective, Literal(True)) in facts:
                        inferred.add((unit, NEXT.usesClient, Literal(True)))
            if relative_path.endswith("layout.tsx") or relative_path.endswith("layout.jsx"):
                for unit in default_units:
                    inferred.add((unit, RDF.type, NEXT.Layout))
                    inferred.add((unit, NEXT.segmentType, Literal("layout")))
            if relative_path.endswith("route.ts") or relative_path.endswith("route.js"):
                for unit in facts.objects(file_iri, LACO.defines):
                    inferred.add((unit, RDF.type, NEXT.APIRoute))
                    inferred.add((unit, NEXT.segmentType, Literal("route")))
                    inferred.add((unit, NEXT.routePattern, Literal(_derive_route_pattern(relative_path))))
    return inferred


def _copy_static_assets(target_vocab: Path, commit_dir: Path) -> None:
    target_vocab.mkdir(parents=True, exist_ok=True)

    def _safe_copy_tree(source_dir: Path, destination: Path, label: str) -> None:
        if not source_dir.exists():
            LOGGER.warning("Static assets directory '%s' not found at %s", label, source_dir)
            return
        for source in source_dir.iterdir():
            if not source.is_file():
                continue
            try:
                shutil.copy2(source, destination / source.name)
            except FileNotFoundError:
                LOGGER.warning("Skipping missing %s asset: %s", label, source)
            except PermissionError as error:
                LOGGER.warning("Unable to copy %s asset %s: %s", label, source, error)

    _safe_copy_tree(VOCAB_ROOT, target_vocab, "vocab")
    shapes_dir = commit_dir / "shapes"
    shapes_dir.mkdir(parents=True, exist_ok=True)
    _safe_copy_tree(SHAPES_ROOT, shapes_dir, "shapes")
    rules_dir = commit_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    _safe_copy_tree(RULES_ROOT, rules_dir, "rules")


@app.command("build")
def build(
    repo: Path = typer.Option(..., exists=True, dir_okay=True, file_okay=False, help="Path to repository root."),
    commit: Optional[str] = typer.Option(None, help="Commit SHA to attribute facts."),
    langs: str = typer.Option("ts", help="Comma-separated languages to extract."),
    nextjs: bool = typer.Option(True, help="Enable Next.js rule extensions."),
    out_dir: Path = typer.Option(Path(".ontology"), help="Output directory relative to repo."),
    emit_inferred: bool = typer.Option(True, help="Run rule packs and write inferred triples."),
    run_shacl: bool = typer.Option(True, help="Execute SHACL validation."),
    lsp_augment: bool = typer.Option(False, help="Augment extraction with LSP cross-file data."),
    max_workers: int = typer.Option(4, min=1, help="Max worker count (reserved for future use)."),
    log_level: str = typer.Option("INFO", help="Log level."),
) -> None:
    configure_logging(log_level)
    repo_path = repo.resolve()

    cli_options: dict[str, object] = {
        "repo": str(repo_path),
        "commit": commit,
        "langs": [part.strip() for part in langs.split(",") if part.strip()],
        "nextjs": nextjs,
        "out_dir": str(out_dir),
        "emit_inferred": emit_inferred,
        "run_shacl": run_shacl,
        "lsp_augment": lsp_augment,
        "max_workers": max_workers,
        "log_level": log_level,
    }

    config = _merge_config(repo_path, cli_options)

    if config.commit:
        commit_sha = config.commit
    else:
        commit_sha = git_rev_parse(str(repo_path))
        config = config.model_copy(update={"commit": commit_sha})

    if config.lsp_augment:
        LOGGER.warning("LSP augmentation is not implemented yet; proceeding without it.")

    out_root = (repo_path / config.out_dir).resolve()
    commit_dir = ensure_out_dir(out_root, commit_sha)
    facts_dir = commit_dir / "facts" / "files"
    inferred_dir = commit_dir / "inferred"
    reports_dir = commit_dir / "reports"
    logs_dir = commit_dir / "logs"
    for directory in (facts_dir, inferred_dir, reports_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    _copy_static_assets(out_root / "vocab", commit_dir)

    ignore_patterns = _collect_matchers(config.ignore)
    language_files = _collect_language_files(repo_path, config.langs, ignore_patterns)
    if not language_files:
        LOGGER.warning("No source files matched the requested languages.")
        return

    include_globs: list[str] = []
    for language in config.langs:
        include_globs.extend(LANGUAGE_GLOBS.get(language, ()))
    include_globs = sorted(dict.fromkeys(include_globs))

    ts_runner = TsExtractorRunner(repo_path)
    extraction_results = ts_runner.run(
        language_files,
        include_globs=include_globs,
        exclude_globs=ignore_patterns,
    )

    if not extraction_results:
        LOGGER.warning("TypeScript extractor produced no facts.")
        return

    repo_name = repo_path.name
    commit_iri = URIRef(f"laco://repo/{repo_name}/commit/{commit_sha}")
    repo_iri = URIRef(f"laco://repo/{repo_name}")

    symbol_index = SymbolIndex(repo_name=repo_name, commit_sha=commit_sha)
    for result in extraction_results:
        if isinstance(result.payload, dict):
            symbol_index.register_payload(result.payload)

    context = MappingContext(
        repo_name=repo_name,
        repo_path=repo_path,
        commit_sha=commit_sha,
        commit_iri=commit_iri,
        repo_iri=repo_iri,
        symbol_index=symbol_index,
    )

    facts_union = Graph()
    facts_union.bind("laco", "https://example.org/laco#")
    facts_union.bind("lasa", "https://example.org/lasa#")
    facts_union.bind("next", "https://example.org/next#")
    facts_union.bind("dct", "http://purl.org/dc/terms/")

    for result in extraction_results:
        payload = result.payload
        if not isinstance(payload, dict):
            continue
        graph = apply_mapping(payload, context)
        facts_union += graph
        relative = payload.get("filePath")
        if not isinstance(relative, str) or not relative:
            continue
        encoded_name = f"{encode_path_for_graph(Path(relative))}.ttl"
        write_graph(graph, facts_dir / encoded_name)

    inferred_graph = Graph()
    if config.emit_inferred:
        rule_files: list[Path] = [RULES_ROOT / "rules-core.rq"]
        if config.nextjs:
            rule_files.append(RULES_ROOT / "rules-next.rq")
        try:
            inferred_graph = run_rule_packs(facts_union, rule_files)
        except Exception as exc:  # pragma: no cover - defensive fallback
            LOGGER.error("Rule packs failed: %s", exc)
            inferred_graph = Graph()
        python_inferred = _python_inference(facts_union)
        inferred_graph += python_inferred
        if len(inferred_graph):
            write_graph(inferred_graph, inferred_dir / "merged.ttl")

    if config.run_shacl:
        validation_graph = Graph()
        validation_graph += facts_union
        if inferred_graph:
            validation_graph += inferred_graph
        shapes: list[Path] = [SHAPES_ROOT / "laco-core.shapes.ttl"]
        if config.nextjs:
            shapes.append(SHAPES_ROOT / "next.shapes.ttl")
        conforms, report = execute_shacl(validation_graph, shapes)
        LOGGER.info("SHACL validation %s", "passed" if conforms else "failed")
        write_graph(report, reports_dir / "shacl_report.ttl")

    LOGGER.info("Extraction complete. Results written to %s", commit_dir)
