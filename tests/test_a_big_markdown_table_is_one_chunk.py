"""A markdown table is one coherent chunk up to a cap, and its source survives.

Ranking and proximity are per chunk, so a table stays whole past the chunk
budget. Past the table cap it is split by rows, and every piece still renders as
a table because it carries the header.
"""

from __future__ import annotations

from pathlib import Path

from fnd.extract import extract
from fnd.extract.base import MAX_CHUNK_CHARS, MAX_TABLE_CHARS


def _table_note(rows: int) -> str:
    header = "| Front | Back | Category |\n| --- | --- | --- |\n"
    body = "".join(
        f"| Question number {i} about the topic "
        f"| A deliberately long answer for row {i} carrying real content {'context ' * 12}"
        f"| Concept |\n"
        for i in range(rows)
    )
    return "# Notes\n\n## Flashcards\n\n" + header + body


def test_a_big_table_is_one_chunk_with_its_source(tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    text = _table_note(60)
    note.write_text(text, encoding="utf-8")
    # Precondition: the table's own text is over the budget, so this exercises
    # the size-bounder rather than a table that fits trivially.
    assert len(text) > MAX_CHUNK_CHARS

    chunks = list(extract(note))
    carrying = [c for c in chunks if "| Front | Back | Category |" in c.body_md]

    assert len(carrying) == 1, (
        "the table must be exactly one chunk carrying its source; "
        f"got {len(carrying)} of {len(chunks)} chunks with source"
    )
    tc = carrying[0]
    # The whole table is present, header to last row, not cut mid-table.
    assert "Question number 0 " in tc.body_md
    assert "Question number 59 " in tc.body_md
    assert tc.body_md.count("\n|") >= 60


def test_a_table_parses_as_cells_not_raw_pipes(tmp_path: Path) -> None:
    """GFM tables are enabled: a small table's searchable body is clean cell
    text, not a wall of pipes."""
    note = tmp_path / "s.md"
    note.write_text(
        "## T\n\n| Term | Meaning |\n| --- | --- |\n| Latency | Time to first byte |\n",
        encoding="utf-8",
    )
    chunks = list(extract(note))
    body = "\n".join(c.body for c in chunks)

    assert "Latency" in body
    assert "Time to first byte" in body
    # The cell text is indexed without the table's pipe scaffolding.
    assert "| Term | Meaning |" not in body


def test_a_small_table_keeps_its_section_together(tmp_path: Path) -> None:
    """A table under budget is not split off from its heading/prose: the section
    stays one chunk so context and ranking hold."""
    note = tmp_path / "m.md"
    note.write_text(
        "## Dims\n\nThe six dimensions:\n\n| Dim | Meaning |\n| --- | --- |\n| Valid | meets rules |\n",
        encoding="utf-8",
    )
    chunks = [c for c in extract(note) if c.body_md.strip()]
    with_table = [c for c in chunks if "| Dim | Meaning |" in c.body_md]

    assert len(with_table) == 1
    assert "The six dimensions:" in with_table[0].body_md, "prose and table split apart"


def test_a_table_past_the_cap_is_split_by_rows_under_its_header(tmp_path: Path) -> None:
    note = tmp_path / "huge.md"
    rows = 400
    note.write_text(_table_note(rows), encoding="utf-8")

    chunks = list(extract(note))
    pieces = [c for c in chunks if "Question number" in c.body_md]

    assert sum(len(c.body) for c in pieces) > MAX_TABLE_CHARS, "precondition: over the cap"
    assert len(pieces) > 1
    assert all(len(c.body) <= MAX_CHUNK_CHARS for c in chunks)
    for c in pieces:
        assert c.body_md.startswith("| Front | Back | Category |\n| --- | --- | --- |\n"), (
            "a piece without its header renders as raw pipes"
        )
    for i in range(rows):
        holding = [c for c in pieces if f"Question number {i} " in c.body_md]
        assert len(holding) == 1, f"row {i} is in {len(holding)} pieces"
