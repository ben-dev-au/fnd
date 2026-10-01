"""Which collection a result came from: marks, the preview's bottom edge, the row suffix."""

from __future__ import annotations

from textual.content import Content

from fnd.query import FileGroup
from fnd.tui.collection_marks import MARK_STYLES, MARKS, PALETTE, CollectionMarks, Mark
from fnd.tui.preview_edge import EDGE_RESERVED, MIN_NAME_CELLS, bottom_edge, marked_name
from fnd.tui.results_labels import _format_file_label, _styled_parent_label, reapply_styles
from fnd.tui.scope_panel import _legend_label

CONFIGURED = ("Work", "Personal", "Archive")


def _marks(*searched: str) -> CollectionMarks:
    return CollectionMarks.build(CONFIGURED, searched)


def _in(*collections: str) -> tuple[tuple[str, str], ...]:
    return tuple((c, f"/{c.lower()}") for c in collections)


def test_one_searched_collection_marks_nothing() -> None:
    m = _marks("Work")
    assert m.shown(_in("Work")) == ()
    assert m.file_mark(_in("Work")) is None
    assert m.mark("Work") is None


def test_marks_go_to_the_searched_collections_in_panel_order() -> None:
    m = _marks("Work", "Archive")
    assert (m.mark("Archive"), m.mark("Work")) == MARKS[:2]


def test_the_first_five_are_colour_alone() -> None:
    assert [mk.shape for mk in MARKS[: len(PALETTE)]] == [""] * len(PALETTE)
    assert all(mk.shape for mk in MARKS[len(PALETTE) :])


def _names(n: int) -> tuple[str, ...]:
    return tuple(f"c{i:03}" for i in range(n))


def test_no_two_marked_collections_share_a_mark() -> None:
    every = _names(100)
    for n in (2, 5, 6, len(MARKS), len(MARKS) + 1, 100):
        searched = every[:n]
        m = CollectionMarks.build(every, searched, results=[_in(c) for c in searched])
        marked = [m.mark(c) for c in searched if m.mark(c)]
        assert len(marked) == len(set(marked)) == min(n, len(MARKS))


def test_past_the_last_mark_only_the_results_are_marked() -> None:
    every = _names(100)
    m = CollectionMarks.build(every, every, results=[_in("c070"), _in("c011"), _in("c003", "c004")])
    assert (m.mark("c011"), m.mark("c070")) == MARKS[:2]
    assert m.mark("c003") is None
    assert m.mark("c000") is None


def test_only_searched_collections_are_shown() -> None:
    assert _marks("Work", "Personal").shown(_in("Archive", "Work")) == ("Work",)


def test_a_shared_file_wears_no_single_mark() -> None:
    m = _marks("Work", "Personal")
    assert m.file_mark(_in("Personal", "Work")) is None
    assert m.file_mark(_in("Work")) == m.mark("Work") is not None


def test_a_partial_collection_claims_only_its_ticked_sources() -> None:
    m = CollectionMarks.build(CONFIGURED, ["Work"], {"Personal": ["/home"]})
    via_vault = (("Personal", "/vault"), ("Work", "/vault"))
    assert m.shown(via_vault) == ("Work",)
    assert m.file_mark(via_vault) == m.mark("Work")
    assert m.shown((("Personal", "/home"),)) == ("Personal",)


def _edge(width: int, name: str, status: str) -> tuple[str, str, int]:
    label, align = bottom_edge(width, Content(name), Content(status))
    return label.plain, align, label.cell_length


def test_name_and_status_span_the_edge() -> None:
    plain, align, cells = _edge(60, "Work", "▲1  ▼2")
    assert align == "left"
    assert cells == 60 - EDGE_RESERVED
    assert plain.startswith("Work ─")
    assert plain.endswith("─ ▲1  ▼2 ")


def test_the_status_sits_in_the_same_cell_with_or_without_a_name() -> None:
    alone, align, _ = _edge(60, "", "▼2")
    assert align == "right"
    assert _edge(60, "Work", "▼2")[0].endswith(alone)


def test_the_name_gives_way_before_the_status() -> None:
    long = "Departmental Meeting Minutes"
    plain, _, cells = _edge(40, long, "▲1  ▼2")
    assert cells == 40 - EDGE_RESERVED
    assert plain.endswith("▲1  ▼2 ")
    assert "…" in plain
    assert long not in plain


def test_a_name_too_short_to_read_is_dropped() -> None:
    status = "◌ match not shown here"
    width = EDGE_RESERVED + len(status) + 4 + MIN_NAME_CELLS - 1
    plain, align, _ = _edge(width, "Work", status)
    assert (plain, align) == (f" {status} ", "right")


def test_a_name_alone_sits_left() -> None:
    assert _edge(60, "Work", "")[:2] == ("Work", "left")


def test_status_alone_keeps_its_old_shape() -> None:
    assert _edge(60, "", "▼2")[:2] == (" ▼2 ", "right")


def _group(path: str) -> FileGroup:
    return FileGroup(parent_id="p", path=path, kind="md", title="", top_score=1.0, hits=[])


def _styled(text: object, start: int) -> str:
    return " ".join(str(s.style) for s in text.spans if s.start <= start < s.end)  # type: ignore[attr-defined]


def test_the_row_suffix_wears_the_colour() -> None:
    label = _format_file_label(_group("/n/minutes.md"), mark=Mark("#87be84"))
    plain: str = label.plain
    assert "#87be84" in _styled(label, plain.rindex(".md"))
    assert "#87be84" not in _styled(label, plain.rindex("minutes"))


def test_an_elided_row_keeps_its_coloured_suffix() -> None:
    label = _format_file_label(
        _group("/n/a-very-long-file-name-for-a-narrow-pane.pdf"),
        name_budget=16,
        mark=Mark("#87be84"),
    )
    assert label.plain.endswith(".pdf")
    assert "#87be84" in _styled(label, len(label.plain) - 1)


def test_a_dimmed_row_leaves_its_suffix_undimmed() -> None:
    from rich.console import Console

    label = _styled_parent_label(_format_file_label(_group("/n/minutes.md"), mark=Mark("#87be84")))
    console = Console()
    assert label.get_style_at_offset(console, 0).dim
    assert not label.get_style_at_offset(console, len(label.plain) - 1).dim


def test_a_shaped_row_keeps_its_budget() -> None:
    shaped = _format_file_label(
        _group("/n/a-very-long-file-name-for-a-narrow-pane.pdf"),
        name_budget=16,
        mark=Mark("#87be84", "■"),
    )
    plain = _format_file_label(
        _group("/n/a-very-long-file-name-for-a-narrow-pane.pdf"), name_budget=16
    )
    assert shaped.plain.endswith(".pdf ■")
    assert shaped.cell_len == plain.cell_len


def test_the_legend_colours_only_the_name() -> None:
    label = "●  Work (2 sources)"
    styled = _legend_label(label, 3, Mark("#e599ca"))
    assert "#e599ca" in _styled(styled, label.index("Work"))
    assert "#e599ca" not in _styled(styled, label.index("(2"))


def test_the_legend_puts_the_shape_after_the_name() -> None:
    styled = _legend_label("●  Work (2 sources)", 3, Mark("#e599ca", "◆"))
    assert styled.plain == "●  Work ◆ (2 sources)"
    assert "#e599ca" in _styled(styled, styled.plain.index("◆"))


def test_the_edge_keeps_the_shape_when_the_name_is_cut() -> None:
    name = marked_name("Departmental Meeting Minutes", Mark("#e599ca", "✚"))
    label, _ = bottom_edge(30, name, Content("▼2"))
    assert label.plain.startswith("✚ Depart")


def test_a_mark_survives_a_row_style_laid_over_it() -> None:
    from rich.console import Console
    from rich.text import Text

    label = _format_file_label(_group("/n/minutes.md"), mark=Mark("#87be84", "■"))
    rendered = Text.assemble("> ", label)
    rendered.stylize("#ffffff")
    reapply_styles(rendered, label, MARK_STYLES)

    def hex_at(offset: int) -> str:
        colour = rendered.get_style_at_offset(Console(), offset).color
        return colour.triplet.hex if colour and colour.triplet else ""

    assert hex_at(len(rendered.plain) - 1) == "#87be84"
    assert hex_at(rendered.plain.index("minutes")) == "#ffffff"
