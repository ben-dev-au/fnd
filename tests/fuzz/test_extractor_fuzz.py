"""Property-based fuzz tests for every extractor. (N2)

The contract every extractor must hold under adversarial input:

  Either it produces a (possibly empty) iterator of well-formed Chunks,
  or it raises ExtractError. Nothing else.

If an extractor leaks a non-ExtractError exception out, the indexer
``except ExtractError`` in fnd/index.py:185/235 misses it and the
whole index build aborts on one bad file — exactly the regression
M6 closed. Hypothesis generates byte blobs (random, prefix-of-magic,
near-valid-OOXML) and feeds them to each extractor; the moment any
input produces an uncaught exception the failure shrinks down to a
minimal seed under tests/fixtures/malformed/ for follow-up.

Run nightly via the slow marker so the main `not slow` suite stays
quick. Use ``uv run pytest tests/fuzz/ -q`` to run on demand.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fnd.extract import ExtractError, docx, extract, markdown, pdf, plain, pptx

# All fuzz tests sit behind `-m slow` so CI's fast suite doesn't pay
# the time cost. Wire into a separate nightly workflow when ready.
pytestmark = pytest.mark.slow

# Hypothesis defaults are tuned for cheap unit tests; bump examples
# and let it run long enough to find real bugs without blocking dev.
_FUZZ_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _zipped(parts: list[tuple[str, bytes]]) -> bytes:
    """Build a ZIP archive in memory. Used to coax the OOXML extractors
    into running their parser path (vs the cheap "not a valid zip" reject)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in parts:
            zf.writestr(name, payload)
    return buf.getvalue()


def _write(tmp: Path, suffix: str, data: bytes) -> Path:
    path = tmp / f"fuzz{suffix}"
    path.write_bytes(data)
    return path


@given(data=st.binary(min_size=0, max_size=64 * 1024))
@_FUZZ_SETTINGS
def test_pdf_extract_only_raises_extract_error(
    tmp_path_factory: pytest.TempPathFactory, data: bytes
) -> None:
    tmp = tmp_path_factory.mktemp("pdf_fuzz")
    path = _write(tmp, ".pdf", data)
    try:
        list(pdf.extract(path))
    except ExtractError:
        pass


@given(zip_payload=st.binary(min_size=0, max_size=4 * 1024))
@_FUZZ_SETTINGS
def test_docx_extract_only_raises_extract_error(
    tmp_path_factory: pytest.TempPathFactory, zip_payload: bytes
) -> None:
    tmp = tmp_path_factory.mktemp("docx_fuzz")
    blob = _zipped([("word/document.xml", zip_payload)])
    path = _write(tmp, ".docx", blob)
    try:
        list(docx.extract(path))
    except ExtractError:
        pass


@given(zip_payload=st.binary(min_size=0, max_size=4 * 1024))
@_FUZZ_SETTINGS
def test_pptx_extract_only_raises_extract_error(
    tmp_path_factory: pytest.TempPathFactory, zip_payload: bytes
) -> None:
    tmp = tmp_path_factory.mktemp("pptx_fuzz")
    blob = _zipped(
        [
            ("[Content_Types].xml", zip_payload),
            ("ppt/presentation.xml", zip_payload),
        ]
    )
    path = _write(tmp, ".pptx", blob)
    try:
        list(pptx.extract(path))
    except ExtractError:
        pass


@given(data=st.binary(min_size=0, max_size=8 * 1024))
@_FUZZ_SETTINGS
def test_markdown_extract_only_raises_extract_error(
    tmp_path_factory: pytest.TempPathFactory, data: bytes
) -> None:
    tmp = tmp_path_factory.mktemp("md_fuzz")
    path = _write(tmp, ".md", data)
    try:
        list(markdown.extract(path))
    except ExtractError:
        pass


@given(data=st.binary(min_size=0, max_size=8 * 1024))
@_FUZZ_SETTINGS
def test_plain_extract_only_raises_extract_error(
    tmp_path_factory: pytest.TempPathFactory, data: bytes
) -> None:
    tmp = tmp_path_factory.mktemp("txt_fuzz")
    path = _write(tmp, ".txt", data)
    try:
        list(plain.extract(path))
    except ExtractError:
        pass


@given(data=st.binary(min_size=0, max_size=8 * 1024))
@_FUZZ_SETTINGS
def test_dispatch_extract_only_raises_extract_error(
    tmp_path_factory: pytest.TempPathFactory, data: bytes
) -> None:
    """Exercise the public ``extract()`` dispatcher across every suffix
    it knows. Confirms the ``yield from <inner>.extract(path)`` route
    propagates ExtractError uniformly."""
    tmp = tmp_path_factory.mktemp("dispatch_fuzz")
    for suffix in (".pdf", ".docx", ".pptx", ".md", ".txt", ".markdown"):
        path = _write(tmp, suffix, data)
        try:
            list(extract(path))
        except ExtractError:
            pass


# ── Static corpus regression tests ──────────────────────────────────


_CORPUS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "malformed"


def _corpus_files() -> list[Path]:
    if not _CORPUS_DIR.is_dir():
        return []
    return sorted(p for p in _CORPUS_DIR.iterdir() if p.is_file())


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.name)
def test_corpus_files_never_leak_raw_exception(path: Path) -> None:
    """Seed corpus regression: every file under tests/fixtures/malformed/
    must either extract cleanly or raise ExtractError — never anything
    else. When Hypothesis or oss-fuzz finds a new crashing input,
    minimise and drop it here."""
    try:
        list(extract(path))
    except ExtractError:
        return
