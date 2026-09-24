"""The outline built from a document's decoded chunks, per file type."""

from __future__ import annotations

from fnd.extract.base import Block
from fnd.query import FileChunk
from fnd.tui.outline_model import NO_HEADINGS, NO_OUTLINE, Outline, OutlineEntry, build_outline


def _chunk(
    seq: int,
    kind: str = "md",
    *,
    blocks: list[Block] | None = None,
    heading_path: str = "",
    body_md: str = "",
    page: int = 0,
    page_label: str = "",
    slide: int = 0,
) -> FileChunk:
    return FileChunk(
        parent_id="doc",
        path="/tmp/doc",
        kind=kind,
        page=page,
        slide=slide,
        heading_path=heading_path,
        chunk_seq=seq,
        blocks=blocks if blocks is not None else [Block("p", "body")],
        page_label=page_label,
        body_md=body_md,
    )


def _h(seq: int, level: int, text: str, kind: str = "md") -> FileChunk:
    return _chunk(seq, kind, blocks=[Block(f"h{level}", text), Block("p", "x")])


def _shape(outline: Outline) -> list[tuple[str, int, int, int]]:
    return [(e.title, e.depth, e.chunk_seq, e.ordinal) for e in outline.entries]


# ── heading blocks (md, docx, odt, html, epub, ods) ──────────────────────


def test_markdown_headings_nest_by_level() -> None:
    chunks = [
        _chunk(0),  # intro before the first heading
        _h(1, 1, "Test Notes"),
        _h(2, 2, "Methodology"),
        _h(3, 3, "Sampling"),
        _h(4, 3, "Analysis"),
        _h(5, 2, "Results"),
    ]
    assert _shape(build_outline(chunks)) == [
        ("Test Notes", 0, 1, 0),
        ("Methodology", 1, 2, 0),
        ("Sampling", 2, 3, 0),
        ("Analysis", 2, 4, 0),
        ("Results", 1, 5, 0),
    ]


def test_a_skipped_level_still_nests_under_the_nearest_shallower_heading() -> None:
    chunks = [_h(0, 1, "A"), _h(1, 3, "C"), _h(2, 2, "B"), _h(3, 4, "D")]
    assert [(e.title, e.depth) for e in build_outline(chunks).entries] == [
        ("A", 0),
        ("C", 1),
        ("B", 1),
        ("D", 2),
    ]


def test_split_pieces_and_empty_headings_add_no_entries() -> None:
    chunks = [
        _h(0, 1, "Big section"),
        _chunk(1, blocks=[Block("p", "second piece of the same section")]),
        _h(2, 2, "   "),
        _h(3, 2, "Next"),
    ]
    assert [e.title for e in build_outline(chunks).entries] == ["Big section", "Next"]


def test_markdown_inline_syntax_is_stripped_from_titles() -> None:
    chunks = [_h(0, 2, "Install **fnd** with [uv](https://x) and `pipx`")]
    assert build_outline(chunks).entries[0].title == "Install fnd with uv and pipx"


def test_non_markdown_titles_are_kept_verbatim() -> None:
    chunks = [_h(0, 1, "snake_case *and* stars", kind="docx")]
    assert build_outline(chunks).entries[0].title == "snake_case *and* stars"


def test_titles_are_single_line_and_display_safe() -> None:
    chunks = [_h(0, 1, "Tab\there", kind="html"), _h(1, 1, "Line\nbreak", kind="html")]
    assert [e.title for e in build_outline(chunks).entries] == ["Tab here", "Line break"]


def test_markup_hostile_titles_survive_verbatim() -> None:
    chunks = [_h(0, 1, "list[str] usage", kind="odt"), _h(1, 1, "Close [/x] tag", kind="odt")]
    assert [e.title for e in build_outline(chunks).entries] == ["list[str] usage", "Close [/x] tag"]


def test_a_document_without_headings_says_so() -> None:
    outline = build_outline([_chunk(0), _chunk(1)])
    assert outline.entries == ()
    assert outline.reason == NO_HEADINGS


def test_spreadsheet_sheets_form_the_outline() -> None:
    chunks = [_h(0, 1, "Budget", kind="ods"), _h(1, 1, "Actuals", kind="ods")]
    assert [e.title for e in build_outline(chunks).entries] == ["Budget", "Actuals"]


# ── kinds without an outline ─────────────────────────────────────────────


def test_code_data_and_plain_text_have_no_outline() -> None:
    for kind in ("python", "rust", "json", "csv", "yaml", "txt"):
        outline = build_outline([_h(0, 1, "looks like a heading", kind=kind)])
        assert outline.entries == (), kind
        assert outline.reason == NO_OUTLINE, kind


def test_no_chunks_is_an_empty_outline() -> None:
    assert build_outline([]).entries == ()


# ── slides ───────────────────────────────────────────────────────────────


def test_slides_list_once_each_with_their_number() -> None:
    chunks = [
        _chunk(0, "pptx", slide=1, heading_path="Welcome"),
        _chunk(1, "pptx", slide=1, heading_path="Welcome"),  # a split piece
        _chunk(2, "pptx", slide=2),
        _chunk(3, "odp", slide=3, heading_path="End"),
    ]
    entries = build_outline(chunks).entries
    assert [(e.title, e.chunk_seq, e.locator, e.depth, e.ordinal) for e in entries] == [
        ("Welcome", 0, "s.1", 0, -1),
        ("Slide 2", 2, "s.2", 0, -1),
        ("End", 3, "s.3", 0, -1),
    ]


# ── PDFs ─────────────────────────────────────────────────────────────────


def test_pdf_bookmarks_build_the_tree_from_heading_paths() -> None:
    chunks = [
        _chunk(0, "pdf", page=1),  # cover: no bookmark yet
        _chunk(1, "pdf", page=2, heading_path="Part I > Chapter 1"),
        _chunk(2, "pdf", page=3, heading_path="Part I > Chapter 1"),
        _chunk(3, "pdf", page=4, heading_path="Part I > Chapter 2", page_label="iv"),
        _chunk(4, "pdf", page=5, heading_path="Part II"),
    ]
    entries = build_outline(chunks).entries
    assert [(e.title, e.depth, e.chunk_seq, e.locator, e.ordinal) for e in entries] == [
        ("Part I", 0, 1, "p.2", -1),
        ("Chapter 1", 1, 1, "p.2", -1),
        ("Chapter 2", 1, 3, "p.iv", -1),
        ("Part II", 0, 4, "p.5", -1),
    ]


def test_a_page_that_opens_with_its_heading_lands_on_that_heading() -> None:
    chunks = [
        _chunk(0, "pdf", page=1, heading_path="A > B", body_md="## B\n\ntext"),
        _chunk(1, "pdf", page=2, heading_path="A > C", body_md="Setext C\n--------\n\ntext"),
        _chunk(2, "pdf", page=3, heading_path="A > D", body_md="text first\n\n## D"),
    ]
    assert [(e.title, e.ordinal) for e in build_outline(chunks).entries] == [
        ("A", 0),
        ("B", 0),
        ("C", 0),
        ("D", -1),
    ]


def test_a_titled_slide_lands_on_its_title() -> None:
    chunks = [
        _chunk(0, "pptx", slide=1, heading_path="Welcome", body_md="# Welcome\n\n- point"),
        _chunk(1, "pptx", slide=2, body_md="- untitled"),
    ]
    assert [e.ordinal for e in build_outline(chunks).entries] == [0, -1]


def test_pdf_split_page_heading_does_not_duplicate_its_bookmark() -> None:
    # A split page carries "bookmark > font heading"; the next page returns to
    # the bare bookmark path, which is already open and adds nothing.
    chunks = [
        _chunk(0, "pdf", page=1, heading_path="Intro"),
        _chunk(1, "pdf", page=1, heading_path="Intro > Scope"),
        _chunk(2, "pdf", page=2, heading_path="Intro"),
        _chunk(3, "pdf", page=3, heading_path=""),
        _chunk(4, "pdf", page=4, heading_path="Intro > Scope"),
    ]
    assert [(e.title, e.depth, e.chunk_seq) for e in build_outline(chunks).entries] == [
        ("Intro", 0, 0),
        ("Scope", 1, 1),
        ("Scope", 1, 4),
    ]


def test_pdf_bookmarks_win_over_texture_headings() -> None:
    chunks = [
        _chunk(0, "pdf", page=1, heading_path="A > B", body_md="# Texture one\n\n## Texture two"),
        _chunk(1, "pdf", page=2, heading_path="A > C", body_md="# Texture three"),
    ]
    assert [e.title for e in build_outline(chunks).entries] == ["A", "B", "C"]


def test_texturised_pdf_without_bookmarks_uses_rendered_headings() -> None:
    chunks = [
        _chunk(
            0, "pdf", page=1, heading_path="Title", body_md="# Title\n\nIntro\n\n## First\n\ntext"
        ),
        _chunk(1, "pdf", page=2, heading_path="Other", body_md="text only"),
        _chunk(
            2, "pdf", page=3, body_md="#\n\nx\n\n### Deep\n\n```\n# not a heading\n```\n\n## Second"
        ),
    ]
    assert _shape(build_outline(chunks)) == [
        ("Title", 0, 0, 0),
        ("First", 1, 0, 1),
        # The empty "#" still counts: the preview renders a heading for it.
        ("Deep", 2, 2, 1),
        ("Second", 1, 2, 2),
    ]


def test_untextured_pdf_without_bookmarks_lists_page_headings() -> None:
    chunks = [
        _chunk(0, "pdf", page=1, heading_path="Introduction"),
        _chunk(1, "pdf", page=2, heading_path="Introduction"),
        _chunk(2, "pdf", page=3, heading_path="Method"),
    ]
    entries = build_outline(chunks).entries
    assert [(e.title, e.depth, e.chunk_seq, e.locator) for e in entries] == [
        ("Introduction", 0, 0, "p.1"),
        ("Method", 0, 2, "p.3"),
    ]


# ── notebooks ────────────────────────────────────────────────────────────


def test_notebook_headings_come_from_markdown_cells_only() -> None:
    chunks = [
        _chunk(0, "ipynb", blocks=[Block("p", "x")], body_md="# Analysis\n\nIntro\n\nSetup\n-----"),
        _chunk(
            1, "ipynb", blocks=[Block("code", "# comment")], body_md="```python\n# comment\n```"
        ),
        _chunk(2, "ipynb", blocks=[Block("p", "x")], body_md="### Results"),
    ]
    assert _shape(build_outline(chunks)) == [
        ("Analysis", 0, 0, 0),
        ("Setup", 1, 0, 1),
        ("Results", 2, 2, 0),
    ]


# ── lookup ───────────────────────────────────────────────────────────────


def _outline(*entries: tuple[str, int, int, int]) -> Outline:
    return Outline(tuple(OutlineEntry(t, d, s, o, at_top=o <= 0) for t, d, s, o in entries))


def test_position_before_the_first_heading_has_no_entry() -> None:
    outline = _outline(("A", 0, 2, 0), ("B", 0, 5, 0))
    assert outline.index_for(0) is None
    assert outline.index_for(1) is None


def test_position_maps_to_the_last_entry_at_or_before_it() -> None:
    outline = _outline(("A", 0, 2, 0), ("B", 0, 5, 0), ("C", 0, 9, 0))
    assert outline.index_for(2) == 0
    assert outline.index_for(4) == 0
    assert outline.index_for(5) == 1
    assert outline.index_for(100) == 2


def test_synthesised_ancestors_resolve_to_the_deepest_entry() -> None:
    outline = _outline(("Part", 0, 3, -1), ("Chapter", 1, 3, -1))
    assert outline.index_for(3) == 1


def test_later_headings_in_a_chunk_need_the_reading_row() -> None:
    outline = _outline(("Prev", 0, 1, 0), ("A", 0, 4, 0), ("B", 1, 4, 1), ("C", 1, 4, 2))
    assert outline.index_for(4) == 1  # no row known: the chunk's first heading
    assert outline.index_for(4, headings_passed=1) == 1
    assert outline.index_for(4, headings_passed=2) == 2
    assert outline.index_for(4, headings_passed=3) == 3


def test_a_heading_below_a_pages_opening_prose_waits_for_the_line() -> None:
    chunks = [
        _chunk(0, "pdf", page=1, body_md="# Methods\n\nprose"),
        _chunk(1, "pdf", page=2, body_md="more methods prose\n\n## Results\n\ntext"),
    ]
    outline = build_outline(chunks)
    assert [(e.title, e.at_top) for e in outline.entries] == [("Methods", True), ("Results", False)]
    assert outline.index_for(1, headings_passed=0) == 0, "still reading the Methods prose"
    assert outline.index_for(1, headings_passed=1) == 1


def test_an_empty_opening_heading_does_not_hide_the_next_one() -> None:
    chunks = [
        _chunk(0, "pdf", page=1, body_md="# Start\n\nx"),
        _chunk(1, "pdf", page=2, body_md="#\n\ncontinuing\n\n## Discussion"),
    ]
    outline = build_outline(chunks)
    assert [(e.title, e.ordinal, e.at_top) for e in outline.entries] == [
        ("Start", 0, True),
        ("Discussion", 1, False),
    ]
    assert outline.index_for(1, headings_passed=2) == 1


def test_a_subtree_holds_its_descendants_only() -> None:
    outline = _outline(
        ("A", 0, 0, 0), ("B", 1, 1, 0), ("C", 2, 2, 0), ("D", 1, 3, 0), ("E", 0, 4, 0)
    )
    assert outline.within(0, 0)
    assert outline.within(0, 2)
    assert outline.within(1, 2)
    assert not outline.within(1, 3)
    assert not outline.within(0, 4)
    assert not outline.within(2, 1)


def test_a_chunk_whose_first_heading_is_below_the_line_falls_back() -> None:
    # Texture page with its first heading further down: ordinal 1+ entries only.
    outline = _outline(("Prev", 0, 1, 0), ("Mid", 0, 4, 1))
    assert outline.index_for(4, headings_passed=0) == 0
    assert outline.index_for(4, headings_passed=2) == 1
