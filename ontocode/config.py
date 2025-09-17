"""Configuration models for OntoCodeIndex."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class LspConfig(BaseModel):
    enable: bool = False
    request_timeout_ms: int = 12_000
    max_restarts: int = 2
    tsserver_log: bool = False


class OntoCodeConfig(BaseModel):
    repo: str
    commit: str | None = None
    langs: List[str] = Field(default_factory=lambda: ["ts"])
    nextjs: bool = True
    out_dir: str = ".ontology"
    emit_inferred: bool = True
    run_shacl: bool = True
    lsp_augment: bool = False
    max_workers: int = 4
    log_level: str = "INFO"
    ignore: List[str] = Field(default_factory=list)
    lsp: LspConfig = Field(default_factory=LspConfig)


__all__ = ["LspConfig", "OntoCodeConfig"]
