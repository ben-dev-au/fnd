"""`1 removed` was true of the collection and silently false of the corpus.

A file in a folder BOTH collections index is marked `no_index`. Updating one
collection reports `Indexed: 0 new  2 already  1 removed`, and the file stays
fully searchable from the other. The user performed a privacy action and got a
numeric receipt that it had worked.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fnd.config import load
from fnd.index import build_index_from_config, collections_still_holding, prune_removed_files
from fnd.tui.indexer_modal import _format_indexed_line


@pytest.fixture
def shared_vault(tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One folder listed under two collections, as the config the app writes."""
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "shared.md").write_text("# Shared\n\nhaystack here.\n", encoding="utf-8")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.Work.sources]]
            path = "{vault.as_posix()}"
            [[collections.Personal.sources]]
            path = "{vault.as_posix()}"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    for name in ("Work", "Personal"):
        build_index_from_config(
            config=cfg.collections[name], collection=name, index_dir=tmp_index_dir
        )
    return vault


def test_a_prune_reports_the_collections_that_still_hold_the_file(
    shared_vault: Path, tmp_index_dir: Path
) -> None:
    """The overlap is written in the config; the receipt must know it."""
    from fnd.query import _open_index

    index = _open_index(tmp_index_dir)
    writer = index.writer(heap_size=15_000_000)
    pruned = prune_removed_files(index, writer, collection="Work", live_parent_ids=set())
    writer.commit()
    assert pruned, "the fixture must have indexed something to prune"

    still = collections_still_holding(index, pruned, excluding="Work")

    assert still == ("Personal",), still


def test_the_line_names_them() -> None:
    """A receipt that cannot be read as 'the corpus is clean'."""
    line = _format_indexed_line(0, 2, 0, 1, ("Personal",))

    assert "1 removed" in line, line
    assert "still in Personal" in line, line


def test_the_line_is_silent_when_nothing_else_holds_it() -> None:
    """The control: no warning when the removal really did empty the corpus."""
    line = _format_indexed_line(0, 2, 0, 1, ())

    assert "1 removed" in line, line
    assert "still in" not in line, line
