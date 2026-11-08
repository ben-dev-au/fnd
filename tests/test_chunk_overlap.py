"""Every window overlap an extractor writes is marked once, so a file's shown chunk
texts rebuild its source exactly."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from fnd.chunk_overlap import mark_repeats, shown_text
from fnd.extract.base import file_parent_id
from fnd.index import build_index
from fnd.matching import MatchSpec
from fnd.query import FileChunk, Searcher
from fnd.render import DIM_STYLES
from fnd.seams import read_seams
from fnd.tui.match_evidence import has_paintable_match
from fnd.tui.widgets.markdown import window_spans


def _indexed(tmp_path: Path, index_dir: Path, name: str, text: str) -> list[FileChunk]:
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / name
    path.write_text(text, encoding="utf-8")
    build_index(roots=[docs], index_dir=index_dir, collection="default")
    return Searcher(index_dir=index_dir).get_file_chunks(file_parent_id(path))


_PROSE = "\n".join(f"line {i} filler words about nothing much" for i in range(400))
_CODE = "\n".join(f"x_{i} = {i}  # row {i}" for i in range(400))
_JSON = "[\n" + ",\n".join(f'  {{"row": {i}}}' for i in range(500)) + "\n]"


@pytest.mark.parametrize(
    ("name", "text"), [("notes.txt", _PROSE), ("prog.py", _CODE), ("rows.json", _JSON)]
)
def test_the_shown_texts_rebuild_the_source(
    tmp_path: Path, tmp_index_dir: Path, name: str, text: str
) -> None:
    """Joined in order, each chunk's text less its repeat is the file, once."""
    chunks = _indexed(tmp_path, tmp_index_dir, name, text)
    assert len(chunks) > 2
    assert all(c.repeat > 0 for c in chunks[:-1])
    assert chunks[-1].repeat == 0
    assert "".join(shown_text(c) for c in chunks) == text


@pytest.mark.parametrize(("name", "text"), [("prog.py", _CODE), ("rows.json", _JSON)])
def test_a_fenced_window_drops_its_repeat_and_seams_both_cuts(
    tmp_path: Path, tmp_index_dir: Path, name: str, text: str
) -> None:
    """The rendered fence holds only the window's own lines, marked as continuing."""
    chunks = _indexed(tmp_path, tmp_index_dir, name, text)
    rendered = [read_seams(c.body_md) for c in chunks]
    source = "\n".join(src.split("\n", 1)[1].rsplit("\n", 1)[0] for _, _, src in rendered)
    assert source == text
    assert rendered[0][0] is None
    assert rendered[-1][1] is None
    for above, below, _ in rendered[1:-1]:
        assert above is not None
        assert above.code
        assert below is not None
        assert below.code


def test_csv_row_groups_repeat_nothing(tmp_path: Path, tmp_index_dir: Path) -> None:
    """Each csv group restates the header by design: that is not an overlap."""
    text = "name,count\n" + "\n".join(f"row{i},{i}" for i in range(120))
    chunks = _indexed(tmp_path, tmp_index_dir, "rows.csv", text)
    assert len(chunks) > 1
    assert all(c.repeat == 0 for c in chunks)


def _extracted(tmp_path: Path, name: str, text: str) -> list[FileChunk]:
    from fnd.extract import code, plain

    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    module = plain if name.endswith(".txt") else code
    return [
        FileChunk(
            parent_id=c.parent_id,
            path=c.path,
            kind=c.kind,
            page=0,
            slide=0,
            heading_path="",
            chunk_seq=c.chunk_seq,
            blocks=c.body_struct,
            body_md=c.body_md,
            line=c.line,
        )
        for c in module.extract(path)
    ]


def test_a_repeat_the_line_numbers_do_not_place_is_kept(tmp_path: Path) -> None:
    """Matching text alone is not an overlap: the windows must also share lines."""
    chunks = _extracted(tmp_path, "prog.py", _CODE)
    assert mark_repeats(chunks)[0].repeat > 0
    shifted = [dataclasses.replace(c, line=c.line + 1000 * i) for i, c in enumerate(chunks)]
    assert all(c.repeat == 0 for c in mark_repeats(shifted))


def test_text_that_differs_from_the_next_window_is_kept(tmp_path: Path) -> None:
    """A txt chunk whose end the next does not start with keeps all of it."""
    chunks = _extracted(tmp_path, "notes.txt", _PROSE)
    assert mark_repeats(chunks)[0].repeat > 0
    edited = [chunks[0], dataclasses.replace(chunks[1], blocks=chunks[2].blocks), *chunks[2:]]
    assert mark_repeats(edited)[0].repeat == 0


@pytest.mark.parametrize("blank", ["\n", " "])
def test_txt_windows_either_side_of_a_skipped_one_share_nothing(tmp_path: Path, blank: str) -> None:
    """A blank window is never indexed, so the chunks around it are not adjacent."""
    chunks = _extracted(tmp_path, "gap.txt", "A" * 900 + blank * 1800 + "B" * 900)
    assert [c.repeat for c in mark_repeats(chunks)] == [200, 0, 200, 0]


def _cut_code(pair: tuple[str, str]) -> str:
    rows = [f"x_{i} = {i}" for i in range(400)]
    rows[147] += f"  # {pair[0]}"
    rows[148] = f"{pair[1]} = 148"
    return "\n".join(rows)


def _painted(chunk: FileChunk, spec: MatchSpec) -> list[tuple[str, bool]]:
    shown = shown_text(chunk).rstrip("\n")
    return [
        (shown[sp.start : sp.end], str(sp.style) not in DIM_STYLES)
        for sp in window_spans(shown, chunk.windows, spec)
    ]


@pytest.mark.parametrize(
    ("query", "first", "second"),
    [
        ('"alpha beta"', [("alpha", True)], [("beta", True)]),
        ("{3} alpha beta", [("alpha", True)], [("beta", True)]),
    ],
)
def test_a_pair_the_cut_runs_through_paints_as_its_window_sees_it(
    tmp_path: Path, query: str, first: list[tuple[str, bool]], second: list[tuple[str, bool]]
) -> None:
    """Row 147 ends one fence and row 148 starts the next; both halves paint full."""
    chunks = mark_repeats(_extracted(tmp_path, "prog.py", _cut_code(("alpha", "beta"))))
    spec = MatchSpec.from_query(query, auto_fuzzy=False)
    assert chunks[0].body_text.split("\n")[147].endswith("alpha")
    assert shown_text(chunks[1]).startswith("beta")
    assert [p for p in _painted(chunks[0], spec) if p[0] != "\n"][-1:] == first
    assert _painted(chunks[1], spec)[:1] == second
    assert has_paintable_match(chunks[0], spec)
    assert has_paintable_match(chunks[1], spec)
