"""The README's `frontmatter` row was wrong twice while the app was right.

It said the rule applies to any file with a frontmatter block whatever the
extension, and that a file without one passes. Measured through the gate the
walk builds: a note without a block is kept OUT (the whole point of the
scoping rule), and a PDF passes because it is out of scope, not because it
lacks a block.
"""

from __future__ import annotations

from pathlib import Path

from fnd.file_facts import FileFacts
from fnd.filters import FileGate, FilterSpec, build_gate
from fnd.filters.dimensions import note_kinds, rule_from_text


def _gate():
    return FileGate.of(
        [
            *build_gate(FilterSpec()).rules,
            rule_from_text("type == 'note'", applies_to=frozenset(note_kinds())),
        ]
    )


def test_a_note_without_frontmatter_is_kept_out(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text("just body\n", encoding="utf-8")

    assert not _gate().passes(FileFacts(tmp_path / "b.md", root=tmp_path))


def test_a_note_that_matches_passes(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("---\ntype: note\n---\n\nbody\n", encoding="utf-8")

    assert _gate().passes(FileFacts(tmp_path / "a.md", root=tmp_path))


def test_a_pdf_is_out_of_scope(tmp_path: Path) -> None:
    (tmp_path / "c.pdf").write_bytes(b"%PDF-1.4\n")

    assert _gate().passes(FileFacts(tmp_path / "c.pdf", root=tmp_path))


def test_the_readme_says_which_kinds() -> None:
    """The row named no kinds at all, so "whatever the extension" was the only
    scope a reader could take from it."""
    readme = Path("README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if line.startswith("| `frontmatter` |"))

    assert ".md" in row, row
    assert ".txt" in row, row
    assert "whatever the extension" not in row, row


def test_the_readme_does_not_promise_a_pass_for_every_blockless_file() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if line.startswith("| `frontmatter` |"))

    assert "A file without one passes" not in row, row
