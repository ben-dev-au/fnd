"""The walker must never descend into an fnd index directory.

Now that .json (and other data files) are indexed, walking a corpus that
contains an fnd index would otherwise ingest the index's own Tantivy
internals (meta.json). Index dirs are identified by their .fnd-schema-version
sidecar and skipped at descent.
"""

from __future__ import annotations

from pathlib import Path

from fnd.walk import walk


def test_walk_skips_fnd_index_directory(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("# hello", encoding="utf-8")
    # A nested fnd index dir with its sidecar + a Tantivy-style meta.json.
    index = tmp_path / "index"
    index.mkdir()
    (index / ".fnd-schema-version").write_text("9", encoding="utf-8")
    (index / "meta.json").write_text('{"schema": []}', encoding="utf-8")

    found = {p.name for p in walk(roots=[tmp_path], skip_dirs=frozenset())}
    assert "doc.md" in found
    assert "meta.json" not in found, "walker indexed fnd's own index internals"


def test_walk_indexes_ordinary_json(tmp_path: Path) -> None:
    """A .json that is *not* inside an index dir is still indexed."""
    (tmp_path / "data.json").write_text("{}", encoding="utf-8")
    found = {p.name for p in walk(roots=[tmp_path], skip_dirs=frozenset())}
    assert "data.json" in found
