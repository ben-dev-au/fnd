"""A ``[...]`` filter asks about frontmatter: it keeps matching notes of every
kind that carries some (.md, .qmd, .txt) and never a file that carries none."""

from __future__ import annotations

from pathlib import Path

from fnd.index import build_index
from fnd.query import Searcher

NOTE = "---\nNotes_Type: Cheat Sheet\n---\n# Sheet\n\nsql injection prevention.\n"


def test_only_notes_whose_frontmatter_matches(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    (root / "sheet.md").write_text(NOTE)
    (root / "sheet.qmd").write_text(NOTE)
    (root / "sheet.txt").write_text(NOTE)
    (root / "other.md").write_text(NOTE.replace("Cheat Sheet", "Lecture"))
    (root / "attack.py").write_text("# sql injection prevention example\nquery = 1\n")
    build_index(roots=[root], index_dir=tmp_index_dir, collection="c")

    hits = Searcher(index_dir=tmp_index_dir).search(
        "sql injection", limit=10, metadata_filter="Notes_Type == 'Cheat Sheet'"
    )
    assert {Path(h.path).name for h in hits} == {"sheet.md", "sheet.qmd", "sheet.txt"}
