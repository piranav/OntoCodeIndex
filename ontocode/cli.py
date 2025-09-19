"""Command-line interface for OntoCodeIndex."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import typer
import yaml
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF, RDFS, XSD

from .config import LspConfig, OntoCodeConfig
from .extract.ts_bridge import TsExtractorRunner
from .git_utils import git_rev_parse
from .logging import configure_logging, get_logger
from .paths import ensure_out_dir, encode_path_for_graph, flatten_relative_path
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
    cleaned_path = relative_path.strip("/")
    if not cleaned_path:
        return "/"

    segments = cleaned_path.split("/")
    in_app = "app" in segments
    route_segments: list[str]
    if in_app:
        # Use the portion after the first "app/" directory for app router entries
        app_index = segments.index("app")
        route_segments = segments[app_index + 1 :]
    elif segments[0] == "pages":
        # Drop the leading "pages" directory for pages router files
        route_segments = segments[1:]
    else:
        route_segments = segments

    normalized: list[str] = []

    for idx, segment in enumerate(route_segments):
        base = re.sub(r"\.(tsx|jsx|ts|js)$", "", segment, flags=re.IGNORECASE)
        if not base:
            continue
        if in_app and base in {"page", "layout", "route", "default"}:
            continue
        if base == "index" and (normalized or idx == len(route_segments) - 1):
            continue

        if base.startswith("[[...") and base.endswith("]]"):
            normalized.append("*" + base[4:-2])
        elif base.startswith("[...") and base.endswith("]"):
            normalized.append("*" + base[4:-1])
        elif base.startswith("[") and base.endswith("]"):
            normalized.append(":" + base[1:-1])
        else:
            normalized.append(base)

    pattern = "/" + "/".join(normalized)
    pattern = re.sub(r"/{2,}", "/", pattern)
    return pattern if normalized else "/"


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

        app_suffix = None
        if "app/" in relative_path:
            app_suffix = relative_path.split("app/", 1)[1]

        if app_suffix:
            lower_suffix = app_suffix.lower()
            if lower_suffix.endswith("page.tsx") or lower_suffix.endswith("page.jsx") or lower_suffix.endswith("page.ts"):
                for unit in default_units:
                    inferred.add((unit, RDF.type, NEXT.Page))
                    inferred.add((unit, NEXT.segmentType, Literal("page")))
                    inferred.add((unit, NEXT.routePattern, Literal(_derive_route_pattern(relative_path))))
                    if (file_iri, TS_EXT.hasUseClientDirective, Literal(True)) in facts:
                        inferred.add((unit, NEXT.usesClient, Literal(True)))
            if lower_suffix.endswith("layout.tsx") or lower_suffix.endswith("layout.jsx"):
                for unit in default_units:
                    inferred.add((unit, RDF.type, NEXT.Layout))
                    inferred.add((unit, NEXT.segmentType, Literal("layout")))
            if lower_suffix.endswith("route.ts") or lower_suffix.endswith("route.js") or lower_suffix.endswith("route.tsx") or lower_suffix.endswith("route.jsx"):
                for unit in facts.objects(file_iri, LACO.defines):
                    inferred.add((unit, RDF.type, NEXT.APIRoute))
                    inferred.add((unit, NEXT.segmentType, Literal("route")))
                    inferred.add((unit, NEXT.routePattern, Literal(_derive_route_pattern(relative_path))))

        pages_suffix = None
        if "pages/" in relative_path:
            pages_suffix = relative_path.split("pages/", 1)[1]

        if pages_suffix and pages_suffix.startswith("api/"):
            for unit in default_units:
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


def _format_timestamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_utc_iso() -> str:
    return _format_timestamp(datetime.now(timezone.utc))


def _relative_to_repo(repo_path: Path, target: Path) -> str:
    try:
        return target.relative_to(repo_path).as_posix()
    except ValueError:
        return target.as_posix()


def _collect_prefixes(graph: Graph) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for prefix, namespace in graph.namespace_manager.namespaces():
        if prefix:
            prefixes[prefix] = str(namespace)
    return prefixes


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def _to_curie(graph: Graph, term: URIRef) -> str:
    try:
        return graph.namespace_manager.normalizeUri(term)
    except Exception:
        return str(term)


def _label_for(graph: Graph, term: URIRef) -> str:
    label = graph.value(term, RDFS.label) or graph.value(term, DCTERMS.title)
    if label is not None:
        return str(label)
    text = str(term)
    if "#" in text:
        return text.rsplit("#", 1)[-1]
    if "/" in text:
        return text.rstrip("/").rsplit("/", 1)[-1]
    return text


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gather_vocab_files(commit_dir: Path, out_root: Path) -> list[Path]:
    candidates = [commit_dir / "vocab", out_root / "vocab"]
    seen: set[Path] = set()
    vocab_paths: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        for item in sorted(candidate.glob("*.ttl")):
            if item in seen:
                continue
            seen.add(item)
            vocab_paths.append(item)
    return vocab_paths


def _emit_mount(
    *,
    repo_path: Path,
    out_root: Path,
    commit_dir: Path,
    commit_sha: str,
    graph_index: list[dict[str, object]],
    facts_union: Graph,
) -> None:
    if not graph_index:
        return
    mount_path = commit_dir / "mount.json"
    facts_dir = commit_dir / "facts" / "files"
    inferred_path = commit_dir / "inferred" / "merged.ttl"
    vocab_paths = _gather_vocab_files(commit_dir, out_root)
    payload: dict[str, object] = {
        "dataset_id": f"ontocode:{repo_path.name}@{commit_sha}",
        "commit_sha": commit_sha,
        "created_at": _now_utc_iso(),
        "union_default_graph": True,
        "facts_dir": _relative_to_repo(repo_path, facts_dir),
        "inferred_file": _relative_to_repo(repo_path, inferred_path),
        "vocab_files": [_relative_to_repo(repo_path, path) for path in vocab_paths],
        "prefixes": _collect_prefixes(facts_union),
        "graph_index": sorted(graph_index, key=lambda entry: str(entry.get("ttl_file", ""))),
        "notes": "Load vocab + all shards + inferred into a single dataset; treat default graph as UNION.",
    }
    _write_json(mount_path, payload)


def _emit_ontology_meta(
    *,
    out_root: Path,
    commit_dir: Path,
    facts_union: Graph,
    inferred_graph: Graph,
    rule_packs: list[dict[str, str]],
) -> None:
    combined = Graph()
    for prefix, namespace in facts_union.namespace_manager.namespaces():
        if prefix:
            combined.bind(prefix, namespace)
    combined += facts_union
    if len(inferred_graph):
        combined += inferred_graph

    class_rows: list[dict[str, object]] = []
    class_query = """
        SELECT ?cls (COUNT(?s) AS ?count)
        WHERE { ?s a ?cls }
        GROUP BY ?cls
        ORDER BY DESC(?count)
    """
    for row in combined.query(class_query):
        cls = row[0]
        if not isinstance(cls, URIRef):
            continue
        count_value = row[1]
        try:
            count = int(count_value.toPython())
        except Exception:  # pragma: no cover - defensive
            continue
        class_rows.append(
            {
                "id": _to_curie(combined, cls),
                "label": _label_for(combined, cls),
                "count": count,
            }
        )
    class_rows.sort(key=lambda entry: (-int(entry["count"]), str(entry["id"])))

    prop_rows = combined.query(
        """
        SELECT DISTINCT ?p (DATATYPE(?o) AS ?dt)
        WHERE { ?s ?p ?o }
        LIMIT 10000
        """
    )
    object_props: dict[URIRef, dict[str, object]] = {}
    data_props: dict[URIRef, dict[str, object]] = {}
    for row in prop_rows:
        prop = row.p
        if not isinstance(prop, URIRef):
            continue
        datatype = row.dt if hasattr(row, "dt") else None
        store = data_props if datatype else object_props
        entry = store.get(prop)
        if entry is None:
            entry = {"id": _to_curie(combined, prop)}
            store[prop] = entry
        domain_term = combined.value(prop, RDFS.domain)
        range_term = combined.value(prop, RDFS.range)
        if datatype:
            entry.setdefault("range", _to_curie(combined, datatype))
            if isinstance(domain_term, URIRef):
                entry.setdefault("on", _to_curie(combined, domain_term))
            if isinstance(range_term, URIRef):
                entry.setdefault("range", _to_curie(combined, range_term))
        else:
            if isinstance(domain_term, URIRef):
                entry.setdefault("domain", _to_curie(combined, domain_term))
            if isinstance(range_term, URIRef):
                entry.setdefault("range", _to_curie(combined, range_term))

    object_properties = [dict(value) for value in object_props.values()]
    data_properties = [dict(value) for value in data_props.values()]
    object_properties.sort(key=lambda entry: str(entry["id"]))
    data_properties.sort(key=lambda entry: str(entry["id"]))

    vocab_versions: dict[str, str] = {}
    for path in _gather_vocab_files(commit_dir, out_root):
        key = f"{path.name}_sha256"
        vocab_versions[key] = _hash_file(path)

    sample_queries = {
        "callable_sample_query": (
            "PREFIX laco:<https://example.org/laco#> PREFIX dct:<http://purl.org/dc/terms/> "
            "SELECT ?fn ?file ?start ?end ?qn WHERE { ?fn a laco:Callable ; laco:qualifiedName ?qn ; "
            "laco:declaredIn ?file ; laco:span [ laco:startLine ?start ; laco:endLine ?end ] . } LIMIT 3"
        ),
        "call_edge_sample_query": (
            "PREFIX laco:<https://example.org/laco#> SELECT ?caller ?callee WHERE { ?caller laco:calls ?callee . } LIMIT 3"
        ),
    }

    payload = {
        "tbox": {
            "classes": class_rows,
            "object_properties": object_properties,
            "data_properties": data_properties,
        },
        "rbox": {"rule_packs": rule_packs},
        "abox_samples": sample_queries,
        "vocabulary_versions": vocab_versions,
    }

    meta_path = commit_dir / "ontology_meta.json"
    _write_json(meta_path, payload)


@app.command("build")
def build(
    repo: Path = typer.Option(..., exists=True, dir_okay=True, file_okay=False, help="Path to repository root."),
    commit: Optional[str] = typer.Option(None, help="Commit SHA to attribute facts."),
    langs: str = typer.Option("ts", help="Comma-separated languages to extract."),
    nextjs: bool = typer.Option(True, help="Enable Next.js rule extensions."),
    out_dir: Path = typer.Option(Path(".ontology"), help="Output directory relative to repo."),
    emit_inferred: bool = typer.Option(True, help="Run rule packs and write inferred triples."),
    emit_mount: bool = typer.Option(True, help="Emit mount.json metadata."),
    emit_meta: bool = typer.Option(True, help="Emit ontology_meta.json metadata."),
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
        "emit_mount": emit_mount,
        "emit_meta": emit_meta,
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
    facts_union.bind("xsd", str(XSD))

    graph_index: list[dict[str, object]] = []

    for result in extraction_results:
        payload = result.payload
        if not isinstance(payload, dict):
            continue
        graph = apply_mapping(payload, context)
        facts_union += graph
        relative = payload.get("filePath")
        if not isinstance(relative, str) or not relative:
            continue
        flattened_name = flatten_relative_path(relative)
        target_path = facts_dir / Path(f"{flattened_name}.ttl")
        write_graph(graph, target_path)
        graph_index.append(
            {
                "ttl_file": target_path.name,
                "source_path": Path(relative).as_posix(),
                "graph_iri": str(context.file_iri(relative)),
                "triples": int(len(graph)),
            }
        )

    rule_pack_events: list[dict[str, str]] = []

    def _record_rule(rule_path: Path, timestamp: datetime) -> None:
        rule_pack_events.append(
            {
                "name": rule_path.name,
                "applied_at": _format_timestamp(timestamp),
            }
        )

    inferred_graph = Graph()
    if config.emit_inferred:
        rule_files: list[Path] = [RULES_ROOT / "rules-core.rq"]
        if config.nextjs:
            rule_files.append(RULES_ROOT / "rules-next.rq")
        try:
            inferred_graph = run_rule_packs(
                facts_union,
                rule_files,
                on_rule_finished=_record_rule,
            )
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

    if config.emit_mount:
        _emit_mount(
            repo_path=repo_path,
            out_root=out_root,
            commit_dir=commit_dir,
            commit_sha=commit_sha,
            graph_index=graph_index,
            facts_union=facts_union,
        )

    if config.emit_meta:
        _emit_ontology_meta(
            out_root=out_root,
            commit_dir=commit_dir,
            facts_union=facts_union,
            inferred_graph=inferred_graph,
            rule_packs=rule_pack_events,
        )

    LOGGER.info("Extraction complete. Results written to %s", commit_dir)
