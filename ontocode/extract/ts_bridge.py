"""Bridge to the embedded TypeScript extractor."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import orjson

from ..logging import get_logger

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class TsExtractionResult:
    file_path: Path
    payload: dict[str, object]


class TsExtractorRunner:
    """Invoke the Node-based TypeScript extractor and collect results."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.node = shutil.which("node")
        self.script = (
            Path(__file__)
            .resolve()
            .parents[1]
            / "resources"
            / "ts_extractor"
            / "dist"
            / "index.js"
        )

    def _ensure_ready(self) -> bool:
        if not self.node:
            LOGGER.warning("Node.js not found on PATH; using fallback extractor.")
            return False
        if not self.script.exists():
            LOGGER.error("Extractor script missing at %s", self.script)
            return False
        return True

    def run(
        self,
        files: Sequence[Path],
        *,
        include_globs: Sequence[str],
        exclude_globs: Sequence[str],
    ) -> list[TsExtractionResult]:
        if not files:
            return []

        if not self._ensure_ready():
            return self._fallback_extract(files)

        rel_files: list[str] = []
        for file_path in files:
            try:
                relative = file_path.relative_to(self.repo)
            except ValueError:
                relative = file_path
            rel_files.append(str(relative.as_posix()))

        env = os.environ.copy()
        env["ONTOCODE_FILE_LIST"] = orjson.dumps(rel_files).decode("utf-8")
        env["ONTOCODE_EXTRACTOR_REPO"] = str(self.repo)

        command = [
            self.node,
            "--no-warnings",
            str(self.script),
            "--repo",
            str(self.repo),
        ]
        if include_globs:
            command.extend(["--files-include", ",".join(include_globs)])
        if exclude_globs:
            command.extend(["--files-exclude", ",".join(exclude_globs)])

        LOGGER.debug("Running TypeScript extractor: %s", " ".join(command))
        process = subprocess.Popen(
            command,
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert process.stdout
        results: list[TsExtractionResult] = []
        try:
            for line in process.stdout:
                payload = line.strip()
                if not payload:
                    continue
                try:
                    data = orjson.loads(payload)
                except orjson.JSONDecodeError:
                    LOGGER.error("Invalid extractor payload: %s", payload)
                    continue
                file_rel = data.get("filePath")
                file_path = self.repo / Path(str(file_rel))
                results.append(TsExtractionResult(file_path=file_path, payload=data))
        finally:
            if process.stdout:
                process.stdout.close()
            stderr_text = process.stderr.read().strip() if process.stderr else ""
            return_code = process.wait()
            if stderr_text:
                log_fn = LOGGER.error if return_code != 0 else LOGGER.debug
                log_fn("TypeScript extractor stderr:\n%s", stderr_text)
            if return_code != 0:
                LOGGER.error("TypeScript extractor failed with exit code %s", return_code)
                if not results:
                    return self._fallback_extract(files)
        if not results:
            LOGGER.warning("Extractor produced no results; using fallback extractor.")
            return self._fallback_extract(files)
        return results

    # --- Fallback implementation -------------------------------------------------

    _FUNCTION_RE = re.compile(
        r"(?P<export>export\s+(?P<default>default\s+)?)?(?P<async>async\s+)?function\s+(?P<name>[A-Za-z0-9_]+)?",
        re.MULTILINE,
    )
    _IMPORT_RE = re.compile(r"^import\s+[^;]+from\s+[\'\"]([^\'\"]+)[\'\"]", re.MULTILINE)
    
    def _fallback_extract(self, files: Sequence[Path]) -> list[TsExtractionResult]:
        LOGGER.warning("Using simplified Python TypeScript extractor; results may be limited.")
        analyzer = _FallbackAnalyzer(self.repo, files)
        return analyzer.extract()


class _FallbackAnalyzer:
    """Very small TypeScript analyzer to keep tests running without Node dependencies."""

    def __init__(self, repo: Path, files: Sequence[Path]) -> None:
        self.repo = repo
        self.files = list(files)
        self.symbol_index: dict[str, str] = {}
        self.payloads: dict[Path, dict[str, object]] = {}

    @staticmethod
    def _module_name(relative: str) -> str:
        return relative.rsplit(".", 1)[0].replace("/", ".")

    @staticmethod
    def _base64_symbol(kind: str, qualified_name: str) -> str:
        raw = f"ts:{kind}:{qualified_name}".encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _span_for(source: str, match_start: int) -> dict[str, int]:
        before = source[:match_start]
        line = before.count("\n") + 1
        last_newline = before.rfind("\n")
        if last_newline == -1:
            col = match_start + 1
        else:
            col = match_start - last_newline
        return {
            "startLine": line,
            "startCol": col,
            "endLine": line,
            "endCol": col + 1,
        }

    def _sha256(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _scan_units(self) -> None:
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(self.repo).as_posix()
            module = self._module_name(rel)
            units: list[dict[str, object]] = []
            for match in TsExtractorRunner._FUNCTION_RE.finditer(text):
                name = match.group("name") or ("default" if match.group("default") else "anonymous")
                qualified = f"{module}.{name}"
                span = self._span_for(text, match.start())
                symbol = self._base64_symbol("callable", qualified)
                unit = {
                    "kind": "callable",
                    "name": name,
                    "qualifiedName": qualified,
                    "symbolId": symbol,
                    "span": span,
                    "astPath": "Fallback/Function",
                    "isExportedDefault": bool(match.group("default")),
                    "isAsync": bool(match.group("async")),
                }
                units.append(unit)
                self.symbol_index[qualified] = symbol
            self.payloads[path] = {
                "filePath": rel,
                "sha256": self._sha256(text),
                "units": units,
                "imports": self._collect_imports(path, text),
                "exports": self._collect_exports(units),
                "occurrences": [],
                "extends": [],
                "implements": [],
                "hasUseClientDirective": self._has_use_client(text),
            }

    def _collect_imports(self, path: Path, text: str) -> list[dict[str, object]]:
        imports: list[dict[str, object]] = []
        directory = path.parent
        for match in TsExtractorRunner._IMPORT_RE.finditer(text):
            spec = match.group(1)
            record: dict[str, object] = {"from": spec, "resolvedKind": "unknown"}
            if spec.startswith("."):
                target = (directory / spec).resolve()
                for suffix in (".ts", ".tsx", ".js", ".jsx"):
                    candidate = target.with_suffix(suffix)
                    if candidate.exists():
                        rel = candidate.relative_to(self.repo).as_posix()
                        record["resolvedKind"] = "file"
                        record["resolved"] = rel
                        break
            elif not spec.startswith("."):
                record["resolvedKind"] = "package"
                record["resolved"] = spec
            imports.append(record)
        return imports

    @staticmethod
    def _collect_exports(units: list[dict[str, object]]) -> list[dict[str, object]]:
        exports: list[dict[str, object]] = []
        for unit in units:
            if unit.get("isExportedDefault"):
                exports.append({"name": "default", "unitSymbolId": unit["symbolId"]})
        return exports

    @staticmethod
    def _has_use_client(text: str) -> bool:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped in {"'use client';", '"use client";', "'use client'", '"use client"'}:
                return True
            break
        return False

    def _collect_occurrences(self) -> None:
        for path, payload in self.payloads.items():
            text = path.read_text(encoding="utf-8")
            module = self._module_name(payload["filePath"])  # type: ignore[index]
            subject_symbol: str | None = None
            for unit in payload["units"]:  # type: ignore[index]
                if unit.get("isExportedDefault"):
                    subject_symbol = unit["symbolId"]  # type: ignore[index]
                    break
            if not subject_symbol:
                continue
            occurrences: list[dict[str, object]] = []
            for match in re.finditer(r"([A-Za-z0-9_]+)\s*\(", text):
                callee = match.group(1)
                if callee == "function":
                    continue
                qualified = f"{callee}"
                # Attempt to resolve against known modules
                for qname, symbol in self.symbol_index.items():
                    if qname.endswith(f".{callee}"):
                        qualified = qname
                        object_symbol = symbol
                        break
                else:
                    object_symbol = None
                span = self._span_for(text, match.start())
                occurrences.append(
                    {
                        "relation": "calls",
                        "subjectSymbolId": subject_symbol,
                        "objectSymbolId": object_symbol,
                        "objectQName": qualified,
                        "span": span,
                    }
                )
            payload["occurrences"] = occurrences  # type: ignore[index]

    def extract(self) -> list[TsExtractionResult]:
        self._scan_units()
        self._collect_occurrences()
        results: list[TsExtractionResult] = []
        for path, payload in self.payloads.items():
            results.append(TsExtractionResult(file_path=path, payload=payload))
        return results


__all__ = ["TsExtractorRunner", "TsExtractionResult"]
