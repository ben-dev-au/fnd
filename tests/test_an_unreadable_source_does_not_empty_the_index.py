"""A source folder that exists but cannot be read wiped the collection.

`chmod 000` a source, press Update index, and 48 documents became 0 while the
screen reported `Done. 0 / 0 files`. The walk swallows the PermissionError and
yields nothing; the prune read that silence as "every file was deleted".

The split was exactly inverted: a source that had MOVED AWAY was protected and
did nothing, while a source present but unreadable acted, and destroyed. Same
panel, same word.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fnd.index import sources_are_enumerable


@pytest.fixture
def unreadable(tmp_path: Path) -> Iterator[Path]:
    """A directory that exists and cannot be listed. Restored so tmp_path can
    be cleaned up even when the assertion fails."""
    d = tmp_path / "locked"
    d.mkdir()
    (d / "note.md").write_text("content\n", encoding="utf-8")
    os.chmod(d, 0o000)
    try:
        yield d
    finally:
        os.chmod(d, stat.S_IRWXU)


def test_an_unreadable_source_is_not_enumerable(unreadable: Path) -> None:
    """The prune is gated on this, and the answer decides whether a mode bit
    can empty a collection."""
    if os.access(unreadable, os.R_OK):
        pytest.skip("running as a user that bypasses directory permissions")

    assert sources_are_enumerable([unreadable]) is False


def test_a_missing_source_is_not_enumerable(tmp_path: Path) -> None:
    """The case that was already protected, kept protected."""
    assert sources_are_enumerable([tmp_path / "gone"]) is False


def test_a_readable_source_is_enumerable(tmp_path: Path) -> None:
    """The control: refusing to prune a healthy source would leave deleted
    files in the index forever."""
    d = tmp_path / "fine"
    d.mkdir()
    (d / "note.md").write_text("content\n", encoding="utf-8")

    assert sources_are_enumerable([d]) is True


def test_an_empty_readable_source_is_still_enumerable(tmp_path: Path) -> None:
    """ "No files" is a real answer and must stay distinguishable from "could
    not look": emptying a source is how a user removes its files."""
    d = tmp_path / "empty"
    d.mkdir()

    assert sources_are_enumerable([d]) is True


def _indexed_paths(index_dir: Path, collection: str) -> set[str]:
    from fnd.index import indexed_parent_ids
    from fnd.query import _open_index

    index = _open_index(index_dir)
    index.reload()
    return indexed_parent_ids(index, collection)


def _seed(tmp_path: Path, index_dir: Path, name: str) -> tuple[Any, Path]:
    from fnd.config import CollectionConfig, SourceConfig
    from fnd.index_runner import run_sync

    root = tmp_path / name
    root.mkdir()
    for leaf in ("a.md", "b.md"):
        (root / leaf).write_text(f"# {leaf}\n\nhaystack\n", encoding="utf-8")
    config = CollectionConfig(sources=[SourceConfig(path=root)])
    run_sync(config=config, collection=name, index_dir=index_dir)
    assert len(_indexed_paths(index_dir, name)) == 2
    return config, root


@pytest.mark.parametrize("rebuild", [False, True])
def test_a_run_over_an_unreadable_source_keeps_the_index(
    rebuild: bool, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Through `run_sync`, the path the TUI and the CLI take.

    Both routes, because a rebuild never reaches the prune: it wipes up front
    and re-adds whatever the walk yields, so an unreadable source empties the
    collection by a different door. Adding or editing a source rebuilds.
    """
    from fnd.index_runner import run_sync

    name = f"vault_{int(rebuild)}"
    config, root = _seed(tmp_path, tmp_index_dir, name)
    before = _indexed_paths(tmp_index_dir, name)

    os.chmod(root, 0o000)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")
        run_sync(config=config, collection=name, index_dir=tmp_index_dir, rebuild=rebuild)
        after = _indexed_paths(tmp_index_dir, name)
    finally:
        os.chmod(root, stat.S_IRWXU)

    assert after == before, f"the run emptied the collection: {before} -> {after}"


@pytest.mark.parametrize("rebuild", [False, True])
def test_the_run_says_which_source_it_could_not_read(
    rebuild: bool, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Keeping the files silently reads exactly like a healthy run."""
    from fnd.index_runner import ProgressEvent, run_sync

    name = f"spoken_{int(rebuild)}"
    config, root = _seed(tmp_path, tmp_index_dir, name)
    seen: list[ProgressEvent] = []

    os.chmod(root, 0o000)
    try:
        if os.access(root, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")
        run_sync(
            config=config,
            collection=name,
            index_dir=tmp_index_dir,
            rebuild=rebuild,
            progress_callback=seen.append,
        )
    finally:
        os.chmod(root, stat.S_IRWXU)

    done = [e for e in seen if e.kind == "done"]
    assert done, [e.kind for e in seen]
    assert done[-1].unreadable_sources == (str(root),), done[-1].unreadable_sources


@pytest.mark.parametrize("rebuild", [False, True])
def test_a_healthy_run_still_prunes_and_stays_quiet(
    rebuild: bool, tmp_path: Path, tmp_index_dir: Path
) -> None:
    """The control, and the one that matters most: a guard that never lets the
    prune run leaves deleted files searchable for ever, and a warning on every
    run is one nobody reads."""
    from fnd.index_runner import ProgressEvent, run_sync

    name = f"healthy_{int(rebuild)}"
    config, root = _seed(tmp_path, tmp_index_dir, name)
    seen: list[ProgressEvent] = []

    (root / "b.md").unlink()
    run_sync(
        config=config,
        collection=name,
        index_dir=tmp_index_dir,
        rebuild=rebuild,
        progress_callback=seen.append,
    )

    assert len(_indexed_paths(tmp_index_dir, name)) == 1
    done = [e for e in seen if e.kind == "done"]
    assert done[-1].unreadable_sources == (), done[-1].unreadable_sources
