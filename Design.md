# OntoCodeIndex Design Overview

This document summarises the production pipeline implemented by the `ontocode` package.

## Architecture layers

- **Extractor** – The embedded Node.js utility uses `ts-morph` to parse TypeScript/JavaScript modules, building JSON records with units, occurrences, imports/exports, and Next.js hints (`hasUseClientDirective`, default export detection, etc.).
- **Python pipeline** – `ontocode.cli.build` orchestrates repository scanning, invokes the extractor, converts JSON to RDF (per file) through a deterministic mapping, and runs rule/shape validation.
- **Ontology assets** – LACO, LASA, and Next.js vocabularies (`ontocode/rdf/vocab/*.ttl`), rules (`ontocode/rdf/rules/*.rq`), and SHACL shapes (`ontocode/rdf/shapes/*.ttl`) are bundled and copied into output folders for traceability.

## Data flow

1. **Configuration** – CLI options merged with `ontocode.yaml` (if present). CLI overrides file values.
2. **Scanning** – Globs per language (`ts` → `**/*.ts`, `**/*.tsx`, etc.) with ignore patterns from config.
3. **Extraction** – `TsExtractorRunner` spawns `node dist/index.js` with the selected files. Results stream as JSONL.
4. **Mapping** – `plugins.typescript.mapping.apply_mapping` translates extractor payloads into RDF triples using stable IRIs:
   - Repository: `laco://repo/<name>`
   - Commit: `laco://repo/<name>/commit/<sha>`
   - File named graphs: `laco://repo/<name>/commit/<sha>/file/<url-encoded-path>`
   - Units: `laco://sym/<repo>/<sha>/<symbolId>`
5. **Materialisation** – Per-file graphs serialised to Turtle. A union graph feeds SPARQL rule packs (core + optional Next.js) to produce inferred triples.
6. **Validation** – `pyshacl` runs with core + Next.js shapes and writes a report graph.

## Extensibility

- **Languages** – Implement `LanguageExtractor` subclasses and new plugins under `ontocode/plugins/<language>/` mapping JSON into RDF. Extend `LANGUAGE_GLOBS` and CLI config to recognise additional languages.
- **Rules & Shapes** – Add new files to `ontocode/rdf/rules` or `ontocode/rdf/shapes`, adjust CLI to include them conditionally.
- **LSP augmentation** – The scaffolding under `ontocode/extract/lsp_client` defines a restartable JSON-RPC client. Future work can gather cross-file references and rename plans.

## Determinism & stability

- Symbol IDs use `base64url("<lang>:<kind>:<qualifiedName>")` to remain stable across runs.
- All paths stored relative to repo root with URL-encoding for named graphs and filenames.
- Rule execution pipeline applies packs sequentially, feeding inferred triples back into the working graph for chained reasoning.
- SHACL validates the combined facts + inferred graph; failing validations appear in `.ontology/commit/<sha>/reports/shacl_report.ttl`.
