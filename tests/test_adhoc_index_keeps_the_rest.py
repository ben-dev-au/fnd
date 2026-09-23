"""`fnd index <root>` adds a root; it does not redefine the collection.

It built a synthetic single-source config and handed it to the same builder a
full reindex uses, so the prune at the end dropped every document in the
collection this one walk had not reached. Seven chunks became one, the command
said "indexed 1 chunks", and it exited 0.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tantivy

from fnd.config import CollectionConfig, SourceConfig
from fnd.index import build_index, build_index_from_config
from fnd.schema import F_COLLECTION


def _count(index_dir: Path, collection: str) -> int:
    index = tantivy.Index.open(str(index_dir))
    index.reload()
    searcher = index.searcher()
    hits = searcher.search(tantivy.Query.all_query(), limit=500).hits
    return sum(1 for _s, a in hits if str(searcher.doc(a).get_first(F_COLLECTION)) == collection)


def _folder(root: Path, name: str, *files: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for f in files:
        (folder / f).write_text(f"# {f}\n\nbody\n", encoding="utf-8")
    return folder


def _adhoc(root: Path, index_dir: Path, collection: str) -> int:
    """What `fnd index <root> --collection <name>` does."""
    return build_index_from_config(
        config=CollectionConfig(sources=[SourceConfig(path=root)]),
        collection=collection,
        index_dir=index_dir,
        prune=False,
    )


def test_a_second_ad_hoc_root_does_not_erase_the_first(tmp_path: Path) -> None:
    index_dir = tmp_path / "idx"
    a = _folder(tmp_path, "a", "one.md", "two.md")
    b = _folder(tmp_path, "b", "three.md")

    _adhoc(a, index_dir, "default")
    assert _count(index_dir, "default") == 2
    _adhoc(b, index_dir, "default")

    assert _count(index_dir, "default") == 3, "the first root's documents survived"


def test_indexing_one_root_into_a_configured_collection_keeps_the_rest(
    tmp_path: Path,
) -> None:
    index_dir = tmp_path / "idx"
    kept = _folder(tmp_path, "kept", "one.md", "two.md")
    extra = _folder(tmp_path, "extra", "three.md")
    build_index_from_config(
        config=CollectionConfig(sources=[SourceConfig(path=kept)]),
        collection="notes",
        index_dir=index_dir,
    )
    assert _count(index_dir, "notes") == 2

    _adhoc(extra, index_dir, "notes")

    assert _count(index_dir, "notes") == 3, "the configured sources were not stale"


def test_a_full_reindex_still_drops_a_deleted_file(tmp_path: Path) -> None:
    """The control: pruning is how a file deleted from disk leaves the index,
    and turning it off where it does not belong must not turn it off here."""
    index_dir = tmp_path / "idx"
    folder = _folder(tmp_path, "notes", "one.md", "two.md")
    config = CollectionConfig(sources=[SourceConfig(path=folder)])
    build_index_from_config(config=config, collection="notes", index_dir=index_dir)
    assert _count(index_dir, "notes") == 2

    (folder / "two.md").unlink()
    build_index_from_config(config=config, collection="notes", index_dir=index_dir)

    assert _count(index_dir, "notes") == 1


def test_the_root_form_still_prunes(tmp_path: Path) -> None:
    """`build_index(roots=...)` is the whole-corpus form and keeps its prune."""
    index_dir = tmp_path / "idx"
    folder = _folder(tmp_path, "notes", "one.md", "two.md")
    build_index(roots=[folder], index_dir=index_dir, collection="c")
    assert _count(index_dir, "c") == 2

    (folder / "two.md").unlink()
    build_index(roots=[folder], index_dir=index_dir, collection="c")

    assert _count(index_dir, "c") == 1


def test_the_command_itself_keeps_the_first_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through `fnd index`, so removing prune=False from the call site fails
    here and not only in the seam below it."""
    from typer.testing import CliRunner

    from fnd.cli import app

    index_dir = tmp_path / "idx"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.cli.default_index_dir", lambda: index_dir)
    a = _folder(tmp_path, "a", "one.md", "two.md")
    b = _folder(tmp_path, "b", "three.md")

    runner = CliRunner()
    first = runner.invoke(app, ["index", str(a)])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["index", str(b)])
    assert second.exit_code == 0, second.output

    assert _count(index_dir, "default") == 3, second.output
