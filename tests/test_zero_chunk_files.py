"""A file that extracts to nothing is never reported as indexed.

Extraction yielding no chunks raises nothing, so every entry point that counts
files has to ask; three image-only PDFs read as "3 newly indexed" against an
index holding none of them. Parameterised over all three so a fourth counting
site fails here until it is handled.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.extract import Chunk
from fnd.index import build_index, build_index_from_config
from fnd.index_runner import run_indexer


def _nothing(path: object, **_kw: object) -> Iterator[Chunk]:
    return iter(())


def _corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "empty.md").write_text("", encoding="utf-8")
    (corpus / "real.md").write_text("# a\n\nbody\n", encoding="utf-8")
    return corpus


@pytest.mark.asyncio
async def test_runner_counts_an_empty_file_as_failed_not_indexed(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    errors: list[str] = []
    done = None
    async for ev in run_indexer(
        config=CollectionConfig(sources=[SourceConfig(path=corpus)]),
        collection="zero",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
    ):
        if ev.kind == "file_error":
            errors.append(ev.error)
        if ev.kind == "done":
            done = ev

    assert done is not None
    assert done.indexed_newly_total == 1, "only real.md reached the index"
    assert done.failed_total == 1
    assert any("empty.md" in e and "no text found" in e for e in errors)


@pytest.mark.asyncio
async def test_runner_names_an_image_only_pdf_for_what_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "scans"
    corpus.mkdir()
    (corpus / "scan.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("fnd.index_runner.extract", _nothing)

    errors: list[str] = []
    async for ev in run_indexer(
        config=CollectionConfig(sources=[SourceConfig(path=corpus)]),
        collection="scans",
        index_dir=tmp_path / "idx",
        state_path=tmp_path / "state.toml",
    ):
        if ev.kind == "file_error":
            errors.append(ev.error)

    assert any("scan.pdf" in e and "image-only" in e for e in errors)


@pytest.mark.parametrize("entry", ["build_index", "build_index_from_config"])
def test_the_sync_builders_do_not_skip_in_silence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    corpus = _corpus(tmp_path)
    index_dir = tmp_path / "idx"
    if entry == "build_index":
        build_index(roots=[corpus], index_dir=index_dir, collection="zero")
    else:
        build_index_from_config(
            config=CollectionConfig(sources=[SourceConfig(path=corpus)]),
            collection="zero",
            index_dir=index_dir,
        )

    err = capsys.readouterr().err
    assert "empty.md" in err
    assert "no text found" in err
    assert "real.md" not in err, "the file that did index must not be reported skipped"
