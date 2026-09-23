"""A folder under a readable source that the walk cannot list keeps its files in
the index, and the run names it, exactly as an unreadable source root does."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.index import _path_parent_id, build_index_from_config, indexed_parent_ids
from fnd.index_runner import ProgressEvent, run_sync
from fnd.query import _open_index


@dataclass(frozen=True, slots=True)
class _Tree:
    config: CollectionConfig
    top: Path
    deleted: Path
    locked: Path
    hidden: Path


def _indexed(index_dir: Path) -> set[str]:
    index = _open_index(index_dir)
    index.reload()
    return indexed_parent_ids(index, "vault")


@pytest.fixture
def tree(tmp_path: Path, tmp_index_dir: Path) -> Iterator[_Tree]:
    """Three indexed files; then one is deleted and the folder holding another goes mode 000."""
    root = tmp_path / "vault"
    locked = root / "locked"
    locked.mkdir(parents=True)
    top = root / "top.md"
    top.write_text("# Top\n\nhaystack\n", encoding="utf-8")
    deleted = root / "deleted.md"
    deleted.write_text("# Deleted\n\nhaystack\n", encoding="utf-8")
    hidden = locked / "inside.md"
    hidden.write_text("# Inside\n\nhaystack\n", encoding="utf-8")
    config = CollectionConfig(sources=[SourceConfig(path=root)])
    build_index_from_config(config=config, collection="vault", index_dir=tmp_index_dir)
    assert len(_indexed(tmp_index_dir)) == 3

    deleted.unlink()
    os.chmod(locked, 0o000)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as a user that bypasses directory permissions")
        yield _Tree(config=config, top=top, deleted=deleted, locked=locked, hidden=hidden)
    finally:
        os.chmod(locked, stat.S_IRWXU)


def test_an_update_keeps_the_files_under_a_folder_it_could_not_list(
    tree: _Tree, tmp_index_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through `build_index_from_config`: the locked folder's file stays, the deleted one goes."""
    build_index_from_config(config=tree.config, collection="vault", index_dir=tmp_index_dir)

    after = _indexed(tmp_index_dir)
    assert _path_parent_id(tree.hidden) in after, "the file under the locked folder was pruned"
    assert _path_parent_id(tree.deleted) not in after, "the prune stopped pruning real deletions"
    assert _path_parent_id(tree.top) in after
    assert str(tree.locked.resolve()) in capsys.readouterr().err


def test_a_run_keeps_the_files_under_a_folder_it_could_not_list_and_names_it(
    tree: _Tree, tmp_index_dir: Path
) -> None:
    """Through `run_sync`: the locked folder's file stays, and the done event names the folder."""
    seen: list[ProgressEvent] = []

    run_sync(
        config=tree.config,
        collection="vault",
        index_dir=tmp_index_dir,
        progress_callback=seen.append,
    )

    after = _indexed(tmp_index_dir)
    assert _path_parent_id(tree.hidden) in after, "the file under the locked folder was pruned"
    assert _path_parent_id(tree.deleted) not in after, "the prune stopped pruning real deletions"
    done = [e for e in seen if e.kind == "done"]
    assert done, [e.kind for e in seen]
    assert done[-1].removed_total == 1
    assert done[-1].unreadable_sources == (str(tree.locked.resolve()),)


@pytest.mark.parametrize("route", ["build_index_from_config", "run_sync"])
def test_a_folder_that_is_really_gone_still_prunes_its_files_quietly(
    route: str, tmp_path: Path, tmp_index_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deleted folder is not an unreadable one: its files leave and nothing is reported."""
    root = tmp_path / "vault"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / "top.md").write_text("# Top\n\nhaystack\n", encoding="utf-8")
    inside = sub / "inside.md"
    inside.write_text("# Inside\n\nhaystack\n", encoding="utf-8")
    config = CollectionConfig(sources=[SourceConfig(path=root)])
    build_index_from_config(config=config, collection="vault", index_dir=tmp_index_dir)
    parent_id = _path_parent_id(inside)
    inside.unlink()
    sub.rmdir()
    seen: list[ProgressEvent] = []

    if route == "run_sync":
        run_sync(
            config=config,
            collection="vault",
            index_dir=tmp_index_dir,
            progress_callback=seen.append,
        )
        assert [e.unreadable_sources for e in seen if e.kind == "done"] == [()]
    else:
        build_index_from_config(config=config, collection="vault", index_dir=tmp_index_dir)
        assert "unreadable" not in capsys.readouterr().err

    assert parent_id not in _indexed(tmp_index_dir)
