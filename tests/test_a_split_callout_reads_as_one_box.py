"""A callout, list or fence cut by a chunk boundary still renders as the one block it is.

Each chunk is its own widget, so the bound marks both sides of the seam and the
preview closes it up: split, the chunks paint exactly what the unsplit source does.
"""

from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
from markdown_it import MarkdownIt
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

import fnd.extract._bound as bound
import fnd.extract.markdown as markdown_extract
from fnd.extract import extract
from fnd.extract.base import Chunk
from fnd.matching import MatchSpec
from fnd.query import FileChunk
from fnd.seams import Seam, read_seams, with_seams
from fnd.tui.app import FNDApp
from fnd.tui.match_evidence import rendered_text
from fnd.tui.preview.frozen import FrozenChunk, stands_in_for
from fnd.tui.preview.presenter import PreviewPresenter
from fnd.tui.widgets.markdown import FNDMarkdown, FNDMarkdownBlockQuote, FNDMarkdownParagraph

_SHAPES = {
    "callout-paragraphs": "> [!note] Title\n> "
    + "\n>\n> ".join(f"para {i} words here" for i in range(8))
    + "\n",
    "callout-list": "> [!tip] T\n" + "".join(f"> - item {i} text\n" for i in range(10)) + "\n",
    "callout-ordered-list": "> [!tip] T\n"
    + "".join(f"> {i + 1}. item {i} text\n>\n>     more {i}\n" for i in range(12))
    + "\n",
    "nested-callout": "> [!note] Outer\n> intro\n>\n> > [!tip] Inner\n"
    + "".join(f"> > line {i} words\n> >\n" for i in range(6))
    + "> outro\n",
    "callout-in-list-item": "- item\n\n  > [!warning] W\n"
    + "".join(f"  > para {i} words\n  >\n" for i in range(6))
    + "\n",
    "fence-in-callout": "> [!example] E\n> `p`\n>\n> ```java\n"
    + "".join(f"> int a{i} = {i};\n" for i in range(20))
    + "> ```\n",
    "plain-fence": "```java\n" + "".join(f"int a{i} = {i};\n" for i in range(20)) + "```\n",
    "fence-in-list-item": "1. step\n\n    ```java\n"
    + "".join(f"    int a{i} = {i};\n" for i in range(20))
    + "    ```\n",
    "ordered-list-from-zero": "> [!tip] T\n>\n"
    + "".join(f"> {i}. item {i} text\n" for i in range(10))
    + "\n",
    "items-opened-on-one-line": "- - first\n\n"
    + "".join(f"    para {i} words here\n\n" for i in range(8)),
    "tab-nested-items": "- outer\n\t- inner\n\n"
    + "".join(f"\t  para {i} words here\n\n" for i in range(8)),
    "fence-in-tab-nested-item": "- outer\n\t- inner\n\n\t  ```py\n"
    + "".join(f"\t  a{i} = {i}\n" for i in range(20))
    + "\t  ```\n",
    "quote-between-items": "- > - first\n"
    + "".join(f"  >\n  >   para {i} words here\n" for i in range(8)),
    "two-callouts": "> [!note] A\n> one two three\n\n> [!tip] B\n> four five six\n",
    "callout-syntax-at-cut": "> [!note] A\n> one two three four\n>\n> [!tip] B\n> five\n",
    "heading-in-callout": "> [!note] T\n> before\n>\n> ## Inside\n> after one\n>\n> after two\n",
}


def _chunks(tmp_path: Path, text: str) -> list[Chunk]:
    note = tmp_path / "n.md"
    note.write_text(text, encoding="utf-8")
    return list(extract(note))


def _budget(monkeypatch: pytest.MonkeyPatch, chars: int) -> None:
    monkeypatch.setattr(bound, "MAX_CHUNK_CHARS", chars)
    monkeypatch.setattr(markdown_extract, "MAX_CHUNK_CHARS", chars)


class _Host(App[None]):
    CSS = FNDApp.CSS

    def __init__(self, sources: list[str]) -> None:
        super().__init__()
        self._sources = sources

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            for source in self._sources:
                yield FNDMarkdown(source, classes="chunk-section chunk-md-body chunk-first")

    def on_mount(self) -> None:
        self.theme = "tokyo-night"


async def _paint(sources: list[str], *, gaps: bool) -> list[tuple[tuple[str, str, str, bool], ...]]:
    """Painted cells as (char, colour, background, bold); ``gaps`` drops blank base rows."""
    app = _Host(sources)
    async with app.run_test(size=(70, 200)) as pilot:
        for md in app.query(FNDMarkdown):
            await md.build_done.wait()
        await pilot.pause()
        rows = [
            tuple(
                (
                    ch,
                    str(seg.style.color) if seg.style and ch.strip() else "",
                    str(seg.style.bgcolor) if seg.style else "",
                    bool(seg.style and seg.style.bold),
                )
                for seg in strip
                for ch in seg.text
            )
            for strip in app.screen._compositor.render_strips()
        ]
    if not gaps:
        return rows
    base = Counter(cell[2] for row in rows for cell in row).most_common(1)[0][0]
    return [row for row in rows if not all(c[0] == " " and c[2] == base for c in row)]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(_SHAPES))
async def test_split_paints_exactly_what_the_unsplit_source_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    text = _SHAPES[shape]
    # Only two separate callouts meet at a boundary that is no seam: the usual gap.
    gaps = shape == "two-callouts"
    whole = await _paint([text], gaps=gaps)
    splits = 0
    for chars in (27, 48, 90):
        _budget(monkeypatch, chars)
        sources = [c.body_md for c in _chunks(tmp_path, text)]
        if len(sources) < 2:
            continue
        splits += 1
        assert all(sources), "a piece fell back to plain lines"
        assert await _paint(sources, gaps=gaps) == whole, (
            f"budget {chars} split {len(sources)} ways"
        )
    assert splits, "precondition: some budget splits the shape"


def test_a_long_paragraph_in_a_callout_splits_by_lines_inside_the_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _budget(monkeypatch, 60)
    text = "> [!note] N\n" + "".join(f"> line {i} of one paragraph\n" for i in range(12))

    chunks = _chunks(tmp_path, text)

    assert len(chunks) > 2
    assert all(c.body_md for c in chunks), "a piece fell back to plain lines"
    for c in chunks[1:]:
        assert read_seams(c.body_md)[0] == Seam(levels=("quote=note",))


def test_every_seam_is_marked_alike_on_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _budget(monkeypatch, 48)
    seams = [read_seams(c.body_md) for c in _chunks(tmp_path, _SHAPES["nested-callout"])]

    assert len(seams) > 2
    for (_, below, _), (above, _, _) in itertools.pairwise(seams):
        assert below == above, "a seam marked differently on its two sides"
    assert any(a and a.levels == ("quote=note", "quote=tip") for a, _, _ in seams)


def test_marks_round_trip_and_compose() -> None:
    above, below = Seam(levels=("list=12", "item"), code=True), Seam(levels=("quote=tip",))
    marked = with_seams(with_seams("> body", above=above), below=below)

    assert read_seams(marked) == (above, below, "> body")
    assert read_seams("> body") == (None, None, "> body")


@dataclass(frozen=True)
class _Body:
    kind: str
    body_md: str
    body_text: str


def test_the_marks_are_never_counted_as_painted_text() -> None:
    marked = with_seams("> code", above=Seam(levels=("quote=example",)))

    assert rendered_text(_Body("md", marked, "code")) == "> code"


@pytest.mark.asyncio
async def test_a_continuation_takes_its_callout_style_with_no_title_of_its_own() -> None:
    source = with_seams("> [!tip] B\n> two", above=Seam(levels=("quote=example",)))
    app = _Host([source])
    async with app.run_test(size=(80, 20)) as pilot:
        await app.query_one(FNDMarkdown).build_done.wait()
        await pilot.pause()

        assert app.query_one(FNDMarkdownBlockQuote).has_class("callout-example")
        assert not any(p.has_class("callout-title") for p in app.query(FNDMarkdownParagraph))


def test_a_capture_still_stands_in_when_only_the_marks_hold_the_query() -> None:
    source = with_seams("> two", above=Seam(levels=("quote=note",)))
    capture = FrozenChunk(
        chunk_seq=0, width=80, strips=[], source=source, painted_match=False, style_key=("t", False)
    )

    assert stands_in_for(capture, source, MatchSpec.from_query("note"), ("t", False))


def test_the_marks_take_no_rows_in_the_context_mounted_above_a_match() -> None:
    seam = Seam(levels=("quote=note",))
    chunks = [
        FileChunk(
            "p", "n.md", "md", 0, 0, "", i, [], body_md=with_seams("> x", above=seam, below=seam)
        )
        for i in range(8)
    ]
    presenter = PreviewPresenter.__new__(PreviewPresenter)

    assert presenter.above_window_start(chunks, focus_idx=7, viewport_h=4) == 3


_LAZY = {
    "quote": "> [!note] T\n> start\n" + "".join(f"lazy line {i} words\n" for i in range(12)),
    "item": "- start of item words\n" + "".join(f"lazy line {i} words\n" for i in range(12)),
    "nested-item": "- outer\n  - inner start words\n"
    + "".join(f"  lazy {i} words here\n" for i in range(12)),
}


def _lazy_levels(source: str) -> set[int]:
    tokens = MarkdownIt("commonmark").parse(source)
    return {
        t.level
        for t, inline in itertools.pairwise(tokens)
        if t.type == "paragraph_open" and "lazy" in inline.content
    }


@pytest.mark.parametrize("shape", list(_LAZY))
def test_a_lazy_continuation_line_is_never_a_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    _budget(monkeypatch, 27)
    text = _LAZY[shape]
    whole = _lazy_levels(text)

    for c in _chunks(tmp_path, text):
        assert _lazy_levels(read_seams(c.body_md)[2]) <= whole, "a lazy line left its container"


_REFERENCES = {
    "one-line-label": "".join(f"> [foo{i}]:\n> /url{i}\n" for i in range(10)),
    "multi-line-label": "".join(f"> [foo{i}\n> bar\n> baz]:\n> /url{i}\n" for i in range(6)),
}


@pytest.mark.parametrize("shape", list(_REFERENCES))
def test_no_line_vanishes_into_a_construct_formed_at_the_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    _budget(monkeypatch, 28)
    text = "> [!note] T\n> start\n" + _REFERENCES[shape]

    # A piece with no source paints its plain text, every line of it.
    for c in (c for c in _chunks(tmp_path, text) if c.body_md):
        tokens = MarkdownIt("commonmark").parse(read_seams(c.body_md)[2])
        painted = " ".join(t.content for t in tokens if t.type == "inline")
        for i in range(10):
            if f"[foo{i}" in c.body:
                assert f"[foo{i}" in painted, f"[foo{i}] vanished"


def test_a_piece_after_plain_lines_still_marks_its_reopened_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _budget(monkeypatch, 48)
    text = "- start of item words\n" + "".join(
        f"  indented {i} words\nlazy {i} words\n" for i in range(8)
    )
    chunks = _chunks(tmp_path, text)

    assert any(not c.body_md for c in chunks), "precondition: a piece fell back to plain lines"
    for c in chunks[1:]:
        if c.body_md:
            assert read_seams(c.body_md)[0] is not None, "a reopened item without its mark"
