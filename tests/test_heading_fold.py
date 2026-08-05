"""``HeadingFolder`` folds a chunk's OWN heading, into every representation.

Pre-fix, every PDF page of a TOC section had that section's heading prepended
to the searchable ``body`` — but never to ``body_md``, which is what the
structural preview renders. Continuation pages therefore matched a heading they
neither owned nor displayed, and selecting one scrolled the preview to text
with no highlight anywhere.
"""

from __future__ import annotations

from fnd.extract.base import Block, Chunk
from fnd.extract.heading_fold import HeadingFolder


def _chunk(heading_path: str, body: str, *, body_md: str = "", seq: int = 0) -> Chunk:
    return Chunk(
        parent_id="p",
        path="/doc.pdf",
        mtime=1,
        kind="pdf",
        body=body,
        body_struct=[Block(kind="p", text=body)],
        body_md=body_md,
        heading_path=heading_path,
        page=seq + 1,
        chunk_seq=seq,
    )


def test_owner_folds_into_body_struct_and_md() -> None:
    """The chunk that starts a section gets the heading in all three."""
    folder = HeadingFolder()
    c = folder.fold(
        _chunk("Introduction > Assessment Test", "Alpha prose.", body_md="Alpha prose.")
    )

    assert c.body.startswith("Assessment Test\n")
    assert c.body_struct[0] == Block(kind="h2", text="Assessment Test")
    assert c.body_md.startswith("## Assessment Test")
    # The original content survives in every representation.
    assert "Alpha prose." in c.body
    assert c.body_struct[-1].text == "Alpha prose."
    assert "Alpha prose." in c.body_md


def test_continuation_chunk_is_not_folded() -> None:
    """Pages 2..N of a section inherit the heading; they must not fold it.

    This is the reported bug: they matched "test" via a heading the preview
    never painted.
    """
    folder = HeadingFolder()
    folder.fold(_chunk("Introduction > Assessment Test", "Page 26 prose.", seq=0))
    cont = folder.fold(_chunk("Introduction > Assessment Test", "Page 27 prose.", seq=1))

    assert cont.body == "Page 27 prose."
    assert all(b.kind != "h2" for b in cont.body_struct)


def test_new_heading_after_a_run_is_folded() -> None:
    """A section boundary re-arms ownership."""
    folder = HeadingFolder()
    folder.fold(_chunk("Ch 1 > Alpha", "one", seq=0))
    folder.fold(_chunk("Ch 1 > Alpha", "two", seq=1))
    c = folder.fold(_chunk("Ch 1 > Beta", "three", seq=2))

    assert c.body.startswith("Beta\n")


def test_folder_state_does_not_span_documents() -> None:
    """A fresh folder per document — otherwise the first chunk of file B
    would be treated as a continuation of file A's last section."""
    first = HeadingFolder().fold(_chunk("Ch 1 > Alpha", "a"))
    second = HeadingFolder().fold(_chunk("Ch 1 > Alpha", "b"))

    assert first.body.startswith("Alpha\n")
    assert second.body.startswith("Alpha\n")


def test_fold_is_idempotent_when_heading_already_present() -> None:
    """No duplication when the extractor already emitted the heading."""
    folder = HeadingFolder()
    c = folder.fold(
        Chunk(
            parent_id="p",
            path="/doc.pdf",
            mtime=1,
            kind="pdf",
            body="Assessment Test\nPage prose.",
            body_struct=[
                Block(kind="h2", text="Assessment Test"),
                Block(kind="p", text="Page prose."),
            ],
            body_md="## Assessment Test\n\nPage prose.",
            heading_path="Introduction > Assessment Test",
        )
    )

    assert c.body.count("Assessment Test") == 1
    assert [b.kind for b in c.body_struct] == ["h2", "p"]
    assert c.body_md.count("Assessment Test") == 1


def test_leaf_matching_is_anchored_not_substring() -> None:
    """A leaf that is a substring of the body's opening word still folds.

    "Security" inside "Cybersecurity" must not read as already-present.
    """
    folder = HeadingFolder()
    c = folder.fold(_chunk("Part 2 > Security", "Cybersecurity is broad."))

    assert c.body.startswith("Security\nCybersecurity")


def test_chunk_without_heading_is_untouched() -> None:
    folder = HeadingFolder()
    c = folder.fold(_chunk("", "Just prose.", body_md="Just prose."))

    assert c.body == "Just prose."
    assert c.body_md == "Just prose."
    assert all(b.kind != "h2" for b in c.body_struct)


def test_empty_body_md_is_left_empty() -> None:
    """Flat-path chunks have no ``body_md``; folding must not create one
    (a non-empty ``body_md`` flips the preview to the structural renderer)."""
    folder = HeadingFolder()
    c = folder.fold(_chunk("Ch 1 > Alpha", "prose", body_md=""))

    assert c.body_md == ""


def test_emphasised_heading_is_recognised_as_already_present() -> None:
    """The structured extractor emits page headings with their typographic
    emphasis intact (``## **Foo**``) while the TOC gives the plain string.
    Comparing them literally folded a second copy in, and the preview showed
    the same heading twice — seen live on the AWS guide's p.24."""
    folder = HeadingFolder()
    c = folder.fold(
        _chunk(
            "Introduction > Interactive Online Learning Environment and Test Bank",
            "prose",
            body_md="## **Interactive Online Learning Environment and Test Bank** \n\nprose",
        )
    )

    assert c.body_md.count("Interactive Online Learning Environment") == 1


def test_case_and_spacing_differences_do_not_duplicate() -> None:
    folder = HeadingFolder()
    c = folder.fold(_chunk("Ch 1 > Alpha Beta", "prose", body_md="##   alpha   beta\n\nprose"))

    assert c.body_md.count("lpha") == 1


def test_searchable_text_the_markdown_lost_is_appended() -> None:
    """A chunk's rendered markdown must not be missing text the index can match.

    FlatFallbackTier repairs a PAGE's markdown but appends the recovered prose
    at the end, headingless; _split_page_sections then slices at ATX headings,
    so the recovery lands in one slice and the other chunks keep what the layout
    parser gave them. Measured on a book-index page: 51 rendered characters of a
    628-character page, every word of it searchable.
    """
    from fnd.extract.pdf import _mirror_body_into_md

    chunk = _chunk(
        "Index",
        "virtual memory, 123\ntransposition sort, 88",
        body_md="**U**\n\n**V**",
    )
    repaired = _mirror_body_into_md(chunk)

    assert "virtual memory, 123" in repaired.body_md
    assert "transposition sort, 88" in repaired.body_md
    assert repaired.body_md.startswith("**U**")  # the parser's own work is kept


def test_mirroring_is_idempotent() -> None:
    """Re-running must not duplicate — the repair also runs on cache-served
    chunks, which may already carry it."""
    from fnd.extract.pdf import _mirror_body_into_md

    chunk = _chunk("Index", "virtual memory, 123", body_md="**V**")
    once = _mirror_body_into_md(chunk).body_md
    twice = _mirror_body_into_md(_mirror_body_into_md(chunk)).body_md

    assert once == twice
    assert twice.count("virtual memory") == 1


def test_a_faithfully_rendered_chunk_is_untouched() -> None:
    from fnd.extract.pdf import _mirror_body_into_md

    chunk = _chunk("Ch 1", "alpha beta gamma", body_md="alpha beta gamma")

    assert _mirror_body_into_md(chunk).body_md == "alpha beta gamma"


def test_flat_chunks_without_markdown_are_untouched() -> None:
    """No body_md means the flat renderer, which already shows the block text."""
    from fnd.extract.pdf import _mirror_body_into_md

    chunk = _chunk("Ch 1", "alpha beta", body_md="")

    assert _mirror_body_into_md(chunk).body_md == ""


def test_dispersed_terms_do_not_stand_in_for_a_phrase() -> None:
    """Presence has to be CONTIGUOUS.

    Asking whether a line's tokens appear anywhere in the markdown let scattered
    words satisfy a phrase: "virtual memory" was treated as present because
    "virtual" and "memory" each occurred elsewhere. A phrase query then matched
    the chunk with no contiguous rendered text for the highlighter to paint —
    the exact failure this repair exists to prevent.
    """
    from fnd.extract.pdf import _mirror_body_into_md

    chunk = _chunk(
        "Index",
        "virtual memory, 123",
        body_md="virtual address space\n\nphysical memory layout",
    )

    assert "virtual memory, 123" in _mirror_body_into_md(chunk).body_md


def test_markup_around_a_rendered_line_is_not_a_difference() -> None:
    """The parser's own formatting must not read as missing text, or every
    emphasised or tabulated line would be appended a second time."""
    from fnd.extract.pdf import _mirror_body_into_md

    emphasised = _chunk("Ch 1", "virtual memory", body_md="**virtual memory**")
    tabulated = _chunk("Ch 1", "alpha beta", body_md="| alpha | beta |")

    assert _mirror_body_into_md(emphasised).body_md == "**virtual memory**"
    assert _mirror_body_into_md(tabulated).body_md == "| alpha | beta |"
