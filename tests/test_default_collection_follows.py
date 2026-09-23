"""defaults.collection never names a collection that is gone.

Renaming or deleting the collection it named left the key dangling. That key
genuinely seeds scope: valid, the TUI boots to 1 of 2 active; dangling, to
2 of 2 and every result. So the user's saved default silently became "search
everything", and only `fnd config validate`, which a tidying user has no
reason to run, said anything.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fnd.config import ALL_COLLECTIONS, delete_collection, load


def _config(tmp_path: Path, default: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(f"""
            [defaults]
            collection = "{default}"

            [[collections.Alpha.sources]]
            path = "/tmp/a"

            [[collections.Beta.sources]]
            path = "/tmp/b"
        """),
        encoding="utf-8",
    )
    return path


def test_a_rename_carries_the_default_over(tmp_path: Path) -> None:
    path = _config(tmp_path, "Alpha")
    assert delete_collection(config_path=path, name="Alpha", renamed_to="Archive") is True
    assert load(path).defaults.collection == "Archive"


def test_a_delete_hands_the_default_back_to_every_collection(tmp_path: Path) -> None:
    path = _config(tmp_path, "Alpha")
    assert delete_collection(config_path=path, name="Alpha") is True
    assert load(path).defaults.collection == ALL_COLLECTIONS


def test_another_collection_going_leaves_the_default_alone(tmp_path: Path) -> None:
    """The control: only the named collection may move the key."""
    path = _config(tmp_path, "Beta")
    delete_collection(config_path=path, name="Alpha")
    assert load(path).defaults.collection == "Beta"
    assert set(load(path).collections) == {"Beta"}


def test_deleting_something_absent_is_still_a_no_op(tmp_path: Path) -> None:
    path = _config(tmp_path, "Beta")
    before = path.read_text(encoding="utf-8")
    delete_collection(config_path=path, name="Gamma")
    assert path.read_text(encoding="utf-8") == before


def test_a_dangling_default_is_repaired_even_with_the_table_gone(tmp_path: Path) -> None:
    """A config restored from backup can already carry the dangle."""
    path = _config(tmp_path, "Gone")
    assert delete_collection(config_path=path, name="Gone") is True
    assert load(path).defaults.collection == ALL_COLLECTIONS
    assert set(load(path).collections) == {"Alpha", "Beta"}
