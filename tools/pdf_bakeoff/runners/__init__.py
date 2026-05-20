"""Runner registry. Each runner is a module exposing NAME, setup(), run()."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from tools.pdf_bakeoff.metrics import RunnerResult


class Runner(Protocol):
    NAME: str

    def setup(self) -> Any: ...
    def run(self, state: Any, pdf_path: Path, page_index: int) -> RunnerResult: ...


# Built-in (no extra deps beyond pyproject)
_BUILTIN: dict[str, str] = {
    "baseline": "tools.pdf_bakeoff.runners.baseline_pymupdf",
    "pymupdf4llm_layout": "tools.pdf_bakeoff.runners.pymupdf4llm_layout",
    "pymupdf4llm_legacy": "tools.pdf_bakeoff.runners.pymupdf4llm_legacy",
    "pymupdf4llm_toc": "tools.pdf_bakeoff.runners.pymupdf4llm_toc",
}

# Opt-in (heavy ML deps; --with-<name>)
_OPTIONAL: dict[str, str] = {
    "docling": "tools.pdf_bakeoff.runners.docling",
    "marker": "tools.pdf_bakeoff.runners.marker",
    "mineru": "tools.pdf_bakeoff.runners.mineru",
}


def all_names() -> list[str]:
    return list(_BUILTIN) + list(_OPTIONAL)


def is_optional(name: str) -> bool:
    return name in _OPTIONAL


def load(name: str) -> Runner:
    mod_path = _BUILTIN.get(name) or _OPTIONAL.get(name)
    if mod_path is None:
        raise KeyError(f"unknown runner: {name!r}")
    return importlib.import_module(mod_path)  # type: ignore[return-value]


def install_hint(name: str) -> str:
    return {
        "docling": "pip install docling",
        "marker": "pip install marker-pdf",
        "mineru": 'uv pip install -U "mineru[all]"',
    }.get(name, "")


def resolve(names: list[str], opt_ins: set[str]) -> list[tuple[str, Runner]]:
    """Return (name, module) pairs, after filtering out opt-ins not requested."""
    resolved: list[tuple[str, Runner]] = []
    for n in names:
        if is_optional(n) and n not in opt_ins:
            continue
        try:
            resolved.append((n, load(n)))
        except ImportError as e:
            hint = install_hint(n)
            extra = f" ({hint})" if hint else ""
            raise SystemExit(f"runner {n!r} not available: {e}{extra}") from e
    return resolved


# Re-exported for convenience
__all__ = ["Runner", "all_names", "install_hint", "is_optional", "load", "resolve"]
_ = Callable  # silence pyright "unused import" when type-only
