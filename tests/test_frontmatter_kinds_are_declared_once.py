"""Which formats carry frontmatter is declared on the kind, once.

The rule's scope and the reader that opens a file to look for a block must
agree. As two expressions kept in step by hand, widening one and not the other
drops a bare `.md` while the identical bare `.txt` beside it sails through.

`KindSpec.carries_frontmatter` is required, so a format added without an
answer is an import error. Everything else derives from it. The tests below
exist so the answer cannot quietly change either.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.file_facts import FileFacts, frontmatter_kinds
from fnd.filters.dimensions import NOTE_KINDS, dimension
from fnd.kinds import ALL_KIND_IDS, FRONTMATTER_KINDS, KIND_BY_ID, kind_for_suffix

#: Reviewed, kind by kind. A new format fails this until someone decides,
#: which is the point: not a list to sync, a decision to make.
_EXPECTED: dict[str, bool] = {
    "pdf": False,
    "docx": False,
    "odt": False,
    "md": True,
    "txt": True,
    "pptx": False,
    "odp": False,
    "json": False,
    "yaml": False,
    "toml": False,
    "xml": False,
    "ini": False,
    "csv": False,
    "tsv": False,
    "ods": False,
    "epub": False,
    "html": False,
    "ipynb": False,
    **dict.fromkeys(
        (
            "python",
            "javascript",
            "typescript",
            "tsx",
            "jsx",
            "c",
            "cpp",
            "csharp",
            "go",
            "rust",
            "java",
            "kotlin",
            "swift",
            "ruby",
            "php",
            "scala",
            "shell",
            "sql",
            "r",
            "lua",
            "perl",
            "dart",
        ),
        False,
    ),
}


def test_every_kind_has_been_asked() -> None:
    """A format added without a reviewed answer fails here."""
    assert set(_EXPECTED) == set(ALL_KIND_IDS), (
        "a kind was added or removed without deciding whether it carries "
        f"frontmatter: {set(ALL_KIND_IDS) ^ set(_EXPECTED)}"
    )


def test_the_registry_answers_the_way_it_was_reviewed() -> None:
    actual = {k: KIND_BY_ID[k].carries_frontmatter for k in ALL_KIND_IDS}
    assert actual == _EXPECTED


def test_nothing_derives_the_answer_a_second_time() -> None:
    """Three names for one fact, and they must all be the same object's word."""
    assert frontmatter_kinds() == FRONTMATTER_KINDS
    assert NOTE_KINDS == FRONTMATTER_KINDS


def test_no_document_format_is_judged_on_metadata_it_cannot_carry() -> None:
    """A PDF, a Word file, a spreadsheet and a notebook all carry metadata of
    their own; none of it is a YAML block, and a filter about one must leave
    them alone rather than drop them."""
    for kind in ("pdf", "docx", "ods", "ipynb", "epub", "html"):
        assert kind not in FRONTMATTER_KINDS, kind


@pytest.mark.parametrize(
    "suffix",
    [".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdwn", ".mdx", ".qmd", ".rmd"],
)
def test_every_markdown_extension_is_markdown(suffix: str) -> None:
    """Named one at a time, because that is how they were missed."""
    assert kind_for_suffix(suffix) == "md", suffix


@pytest.mark.parametrize("suffix", [".md", ".markdown", ".qmd", ".rmd", ".txt"])
def test_a_bare_file_of_any_such_kind_fails_a_frontmatter_rule(tmp_path: Path, suffix: str) -> None:
    """The bug itself, over every extension it could hide behind."""
    path = tmp_path / f"bare{suffix}"
    path.write_text("no block here\n", encoding="utf-8")
    rule = dimension("frontmatter").rule("Course == 'Unstructured Data'")

    assert rule is not None
    assert not rule.passes(FileFacts(path, root=tmp_path)), suffix


def test_and_a_pdf_is_still_not_judged(tmp_path: Path) -> None:
    """The control this whole scope exists to protect."""
    path = tmp_path / "lecture.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    rule = dimension("frontmatter").rule("Course == 'Unstructured Data'")

    assert rule is not None
    assert rule.passes(FileFacts(path, root=tmp_path))
