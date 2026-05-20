"""Verify fnd's PDF extractor works without the pdf-structure extra.

Requirements covered:
- F1: Without `pdf-structure` extra, `fnd` works byte-identically to today.
- NF1: Zero behavioural change for users not opting in.
- NF7: No pymupdf4llm imports happen at fnd startup when extra is absent.

These tests run in the *project's* venv, which (as of Phase 1 step 1a)
does NOT include pymupdf4llm — moved to `[pdf-structure]` extras group.
The tests assert the absence directly so we'd notice if a future change
silently re-added it as a hard dependency.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"


def test_pymupdf4llm_not_in_venv() -> None:
    """NF1 / NF7: pymupdf4llm must not be installed in the base venv."""
    assert importlib.util.find_spec("pymupdf4llm") is None, (
        "pymupdf4llm is reachable from fnd's venv. It should live only in the "
        "[pdf-structure] extras group. Move it back to optional-dependencies."
    )


def test_fnd_extract_pdf_imports_without_pymupdf4llm() -> None:
    """F1 / NF7: importing the production PDF extractor must succeed
    without pymupdf4llm being present."""
    # Force a fresh import so cached module state from earlier tests doesn't lie.
    for mod_name in [m for m in sys.modules if m.startswith("fnd.extract.pdf")]:
        del sys.modules[mod_name]
    mod = importlib.import_module("fnd.extract.pdf")
    assert hasattr(mod, "extract"), "fnd.extract.pdf must expose extract()"


def test_extract_runs_end_to_end_on_fixture() -> None:
    """F1: extract() yields chunks on the existing fixture without the extra."""
    assert FIXTURE.exists()
    from fnd.extract.pdf import extract

    chunks = list(extract(FIXTURE))
    assert chunks, "extract() must yield at least one chunk"
    for c in chunks:
        assert c.kind == "pdf"
        assert c.body, "every chunk must have non-empty body text"
        # NF1: in flat mode, body_md must remain empty so the preview
        # dispatcher keeps PDFs on the current flat path.
        assert c.body_md == "", (
            "body_md must be empty in flat extraction mode; populated only "
            "when the pdf-structure extra is installed (Phase 1 step 1b+)"
        )


@pytest.mark.parametrize("modname", ["pymupdf4llm", "docling"])
def test_optional_extractors_not_imported_eagerly(modname: str) -> None:
    """NF7: importing fnd.extract.pdf must not eagerly load the optional
    extractors. Verifies they stay out of import-time cost for users
    who haven't opted in."""
    # Clear any prior import state.
    for k in list(sys.modules):
        if modname in k or k.startswith("fnd.extract.pdf"):
            sys.modules.pop(k, None)
    importlib.import_module("fnd.extract.pdf")
    assert (
        modname not in sys.modules
    ), f"{modname} was imported at fnd.extract.pdf import time — should be lazy"
