"""A frontmatter rule judges a note that has no block, and drops it.

Skipping such a file turned "index this course" into "index everything except
other courses": every untagged note in a vault reached the index, and the
search that found them named a course filter it had never applied.

The rule still must not judge a PDF, which cannot answer the question, and it
must judge any file that does carry a block, whatever its extension.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.file_facts import FileFacts
from fnd.filters.dimensions import dimension
from fnd.walk import walk_sources

_RULE = "Course == 'Unstructured Data' AND NOT ('private' in tags)"


def _facts(path: Path, root: Path) -> FileFacts:
    return FileFacts(path, root=root)


@pytest.mark.parametrize("name", ["bare.md", "bare.txt"])
def test_a_note_with_no_block_is_dropped(tmp_path: Path, name: str) -> None:
    """Every kind that CAN carry a block, not Markdown alone.

    The scope was narrowed to `md` by hand while the reader that decides
    whether a file can carry a block reads the whole notes category, so a
    bare `.txt` was out of scope and sailed past the rule that dropped the
    identical `.md` beside it.
    """
    (tmp_path / name).write_text("nothing declared\n", encoding="utf-8")
    rule = dimension("frontmatter").rule(_RULE)

    assert rule is not None
    assert not rule.passes(_facts(tmp_path / name, tmp_path))


def test_the_scope_is_whatever_can_carry_a_block() -> None:
    """One function decides it, so the two cannot drift apart again."""
    from fnd.file_facts import frontmatter_kinds
    from fnd.filters.dimensions import NOTE_KINDS

    assert NOTE_KINDS == frontmatter_kinds()
    assert "txt" in NOTE_KINDS


def test_a_note_that_answers_the_rule_is_kept(tmp_path: Path) -> None:
    (tmp_path / "wk2.md").write_text(
        "---\nCourse: Unstructured Data\ntags: []\n---\n\nnotes\n", encoding="utf-8"
    )
    (tmp_path / "other.md").write_text(
        "---\nCourse: Software Design\ntags: []\n---\n\nnotes\n", encoding="utf-8"
    )
    rule = dimension("frontmatter").rule(_RULE)

    assert rule is not None
    assert rule.passes(_facts(tmp_path / "wk2.md", tmp_path))
    assert not rule.passes(_facts(tmp_path / "other.md", tmp_path))


def test_a_pdf_is_still_not_judged(tmp_path: Path) -> None:
    """The control the skip existed for: strict null on a file that cannot
    carry frontmatter would drop every PDF in the source."""
    (tmp_path / "lecture.pdf").write_bytes(b"%PDF-1.4\n")
    rule = dimension("frontmatter").rule(_RULE)

    assert rule is not None
    assert rule.passes(_facts(tmp_path / "lecture.pdf", tmp_path))


def test_the_walk_leaves_the_untagged_note_out(tmp_path: Path) -> None:
    """End to end through the walk, which is where the index gets its files."""
    from fnd.config import SourceConfig

    root = tmp_path / "vault"
    root.mkdir()
    (root / "bare.md").write_text("# Daily\n\nno block\n", encoding="utf-8")
    (root / "wk2.md").write_text(
        "---\nCourse: Unstructured Data\ntags: []\n---\n\nnotes\n", encoding="utf-8"
    )
    (root / "lecture.pdf").write_bytes(b"%PDF-1.4\n")
    source = SourceConfig.model_validate({"path": str(root), "filters": {"frontmatter": _RULE}})

    found = {p.name for p in walk_sources(sources=[source])}

    assert "wk2.md" in found, found
    assert "lecture.pdf" in found, "a PDF cannot answer the rule and must not be judged"
    assert "bare.md" not in found, "an untagged note reached the index anyway"


def test_a_rule_naming_frontmatter_and_a_file_fact_still_spares_a_pdf(tmp_path: Path) -> None:
    """A mixed rule was sent to the unscoped channel, where the frontmatter
    half strict-nulled on every PDF and took the whole clause down with it.

    The wrinkle this leaves, stated deliberately: `file.size < 10` alone drops
    the PDF, and `Course == 'X' OR file.size < 10` keeps it. A rule the file
    cannot answer passes, which is the policy everywhere else in the set.
    """
    from fnd.config import SourceConfig
    from fnd.walk import walk_sources

    root = tmp_path / "vault"
    root.mkdir()
    (root / "wk2.md").write_text("---\nCourse: Unstructured Data\n---\n\nnotes\n", encoding="utf-8")
    (root / "other.md").write_text("---\nCourse: Software Design\n---\n\nnotes\n", encoding="utf-8")
    (root / "lecture.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 5000)
    source = SourceConfig.model_validate(
        {
            "path": str(root),
            "filters": {"frontmatter": "Course == 'Unstructured Data' OR file.size < 10"},
        }
    )

    found = {p.name for p in walk_sources(sources=[source])}

    assert "lecture.pdf" in found, "a 5 KB PDF vanished under a rule about a course"
    assert "wk2.md" in found, found
    assert "other.md" not in found, "the rule still has to bite where it can"
