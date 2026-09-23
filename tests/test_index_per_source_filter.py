"""End-to-end build with one filtered md source + one pdf source."""

from __future__ import annotations

from pathlib import Path

from fnd.config import CollectionConfig, SourceConfig
from fnd.index import build_index_from_config
from fnd.query import Searcher


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_only_matching_md_files_indexed(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    _touch(
        notes / "in_scope.md",
        "---\nCourse: DPwC\n---\n# Note\npenguin sandwich\n",
    )
    _touch(
        notes / "out_of_scope.md",
        "---\nCourse: Algorithms\n---\n# Other\npenguin sandwich\n",
    )
    cc = CollectionConfig(
        sources=[
            SourceConfig(
                path=notes,
                includes=["**/*.md"],
                frontmatter_filter="Course == 'DPwC'",
            )
        ]
    )
    written = build_index_from_config(config=cc, collection="coursework", index_dir=tmp_index_dir)
    assert written >= 1
    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("penguin sandwich", limit=10, collection="coursework")
    paths = {Path(h.path).name for h in hits}
    assert "in_scope.md" in paths
    assert "out_of_scope.md" not in paths


def test_legacy_flat_shape_still_indexes(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "papers"
    _touch(root / "a.md", "# A\nblue penguin sandwich\n")
    cc = CollectionConfig(
        roots=[root],
        includes=["**/*.md"],
    )
    written = build_index_from_config(config=cc, collection="papers", index_dir=tmp_index_dir)
    assert written >= 1
    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("penguin", limit=5, collection="papers")
    assert any(Path(h.path).name == "a.md" for h in hits)
