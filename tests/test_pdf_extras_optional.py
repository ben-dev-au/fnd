"""Verify fnd's PDF extractor works without the pdf-structure extra.

Requirements covered:
- F1: Without `pdf-structure` extra, `fnd` works byte-identically to today.
- NF1: Zero behavioural change for users not opting in.
- NF7: No pymupdf4llm imports happen at fnd startup when extra is absent.

The first two assertions only hold when the extra is *absent* from the
venv, so those tests are skipped when pymupdf4llm is importable
(i.e., after `uv sync --extra pdf-structure`). CI runs without the
extra and exercises the strict invariant; developers who've installed
the extra can still run the rest.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"

_HAS_PYMUPDF4LLM = importlib.util.find_spec("pymupdf4llm") is not None
_extras_absent_only = pytest.mark.skipif(
    _HAS_PYMUPDF4LLM, reason="extra installed; this test asserts the no-extras invariant"
)


@_extras_absent_only
def test_pymupdf4llm_not_in_venv() -> None:
    """NF1 / NF7: pymupdf4llm must not be installed in the base venv."""
    assert importlib.util.find_spec("pymupdf4llm") is None, (
        "pymupdf4llm is reachable from fnd's venv. It should live only in the "
        "[pdf-structure] extras group."
    )


def test_fnd_extract_pdf_imports() -> None:
    """F1 / NF7: importing the production PDF extractor must succeed
    regardless of whether the extra is installed."""
    for mod_name in [m for m in sys.modules if m.startswith("fnd.extract.pdf")]:
        del sys.modules[mod_name]
    mod = importlib.import_module("fnd.extract.pdf")
    assert hasattr(mod, "extract")


def test_extract_runs_end_to_end_on_fixture() -> None:
    """F1: extract() yields chunks on the existing fixture regardless of extra."""
    assert FIXTURE.exists()
    from fnd.extract.pdf import extract

    chunks = list(extract(FIXTURE))
    assert chunks
    for c in chunks:
        assert c.kind == "pdf"
        assert c.body, "every chunk must have non-empty body text"


@_extras_absent_only
def test_body_md_empty_without_extra() -> None:
    """NF1: in flat mode (no extra), body_md must be empty so the
    preview dispatcher keeps PDFs on the flat path."""
    from fnd.extract.pdf import extract

    for c in extract(FIXTURE):
        assert c.body_md == "", (
            f"body_md must be empty without the pdf-structure extra; got {c.body_md[:80]!r}"
        )


@pytest.mark.parametrize("modname", ["pymupdf4llm", "docling"])
def test_optional_extractors_not_imported_eagerly(modname: str) -> None:
    """NF7: importing fnd.extract.pdf must not eagerly load the optional
    extractors. Verifies they stay out of import-time cost for users
    who haven't opted in."""
    for k in list(sys.modules):
        if modname in k or k.startswith("fnd.extract.pdf"):
            sys.modules.pop(k, None)
    importlib.import_module("fnd.extract.pdf")
    assert modname not in sys.modules, (
        f"{modname} was imported at fnd.extract.pdf import time — should be lazy"
    )
