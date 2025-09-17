# OntoCodeIndex

OntoCodeIndex converts a source repository into RDF knowledge based on the Language‑Agnostic Code Ontology (LACO), Language‑Agnostic Semantic Annotations (LASA), and a Next.js extension model. The package ships a deterministic pipeline:

- Walk a repository with rule-driven ignore patterns
- Invoke an embedded TypeScript/JavaScript extractor (ts-morph powered)
- Materialise per-file named graphs in Turtle under `.ontology/commit/<sha>/facts/files/`
- Execute SPARQL CONSTRUCT packs (core + Next.js) to infer LASA/Next semantics
- Validate the union graph using SHACL (core + Next.js)

## Quick start

```bash
pip install .
ontocode build --repo path/to/repo --commit HEAD --nextjs true --emit-inferred true --run-shacl true
```

Outputs are written to `<repo>/.ontology/…`:

```
.ontology/
  vocab/                 # copies of laco.ttl, lasa.ttl, next.ttl
  commit/<sha>/
    facts/files/*.ttl    # per-file named graphs
    inferred/merged.ttl  # rule materialisation
    rules/               # rule packs snapshot
    shapes/              # SHACL shapes snapshot
    reports/shacl_report.ttl
    logs/
```

A configuration file (`ontocode.yaml`) placed at the repository root can override defaults:

```yaml
langs: ["ts"]
nextjs: true
ignore:
  - "node_modules/**"
  - ".next/**"
lsp:
  enable: true
  request_timeout_ms: 12000
```

## Dependencies

- Python ≥ 3.11
- Node.js ≥ 18 with `typescript`, `ts-morph`, `fast-glob`
- Git (optional – pass `--commit` to avoid calling git)

## Development

```
pip install .[dev]
pytest
ruff check .
mypy ontocode
```

The embedded TypeScript extractor lives in `ontocode/resources/ts_extractor`. It can be built standalone via `npm install && npm run build`, although the repository bundles a precompiled `dist/` for convenience.
