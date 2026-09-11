"""No extractor can ship a chunk over the budget, because none has the option.

Two producers were bounded by hand and every other one was left able to make
the same defect: a markdown section with no subheading, a CSV of enormous
rows, any kind added later. The contract now lives at the dispatcher every
extractor's output passes through.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from fnd.extract import extract
from fnd.extract._bound import bounded
from fnd.extract.base import MAX_CHUNK_CHARS, Block, Chunk


def _chunk(blocks: list[Block], **over: object) -> Chunk:
    fields: dict[str, object] = {
        "parent_id": "p",
        "path": "/x/y.md",
        "mtime": 1,
        "kind": "md",
        "body": "\n".join(b.text for b in blocks),
        "body_struct": blocks,
        "page": 12,
        "line": 40,
        "heading_path": "A > B",
        "chunk_seq": 7,
    }
    fields.update(over)
    return Chunk(**fields)  # type: ignore[arg-type]


def test_an_oversized_chunk_is_cut_at_block_boundaries() -> None:
    blocks = [Block("p", "para " * 700)] * 4  # 3,500 chars each, 14k in all

    pieces = list(bounded([_chunk(blocks)]))

    assert len(pieces) == 2, [len(p.body) for p in pieces]
    assert all(len(p.body) <= MAX_CHUNK_CHARS for p in pieces)
    assert [len(p.body_struct) for p in pieces] == [2, 2], "a piece started mid-block"


def test_a_lone_block_over_the_budget_is_cut_at_whitespace() -> None:
    pieces = list(bounded([_chunk([Block("p", "word " * 5_000)])]))

    assert len(pieces) > 1
    assert all(len(p.body) <= MAX_CHUNK_CHARS for p in pieces)
    assert all(not p.body.startswith("ord ") for p in pieces), "a term was halved"


def test_pieces_keep_the_deep_link_and_are_renumbered() -> None:
    chunks = [_chunk([Block("p", "x " * 6_000)]), _chunk([Block("p", "short")], chunk_seq=99)]

    pieces = list(bounded(chunks))

    assert [p.chunk_seq for p in pieces] == list(range(len(pieces)))
    assert all((p.page, p.line, p.heading_path) == (12, 40, "A > B") for p in pieces[:-1])


def test_a_chunk_under_the_budget_passes_through_untouched() -> None:
    """The control: the guarantee costs an in-budget chunk nothing but its seq."""
    blocks = [Block("h2", "Title"), Block("p", "body")]
    original = _chunk(blocks, body_md="## Title\n\nbody\n")

    (piece,) = bounded([original])

    assert piece is original
    assert piece.body_md == "## Title\n\nbody\n"


def test_the_source_is_sliced_for_each_piece_or_left_empty() -> None:
    """The preview prefers verbatim markdown; a piece carries the part behind
    its own blocks, or nothing, and nothing means the block renderer."""
    paras = [f"paragraph {i} " + "text " * 700 for i in range(4)]  # 3.5k each, two per piece
    source = "\n\n".join(paras) + "\n"
    blocks = [Block("p", t) for t in paras]

    pieces = list(bounded([_chunk(blocks, body_md=source)]))

    assert len(pieces) == 2
    assert pieces[0].body_md.startswith("paragraph 0 ")
    assert "paragraph 2" not in pieces[0].body_md
    assert pieces[1].body_md.startswith("paragraph 2 ")


def test_a_rendering_the_source_cannot_be_found_in_yields_no_source() -> None:
    blocks = [Block("p", "rendered " * 1_100)] * 2

    pieces = list(bounded([_chunk(blocks, body_md="# entirely different\n")]))

    assert all(p.body_md == "" for p in pieces)


def test_the_dispatcher_bounds_whatever_an_extractor_yields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, for every kind at once: a stand-in extractor that yields one
    giant chunk still comes out of `extract()` bounded."""
    import sys

    from fnd import extract as pkg

    giant = _chunk([Block("p", "w " * 20_000)], kind="fake")
    fake = SimpleNamespace(extract=lambda path: iter([giant]))
    monkeypatch.setitem(sys.modules, "fnd.extract.fakekind", fake)
    monkeypatch.setitem(pkg._DISPATCH, ".fake", "fakekind")
    f = tmp_path / "a.fake"
    f.write_text("x")

    out = list(extract(f))

    assert len(out) > 1
    assert all(len(c.body) <= MAX_CHUNK_CHARS for c in out)


def _long_markdown(path: Path) -> None:
    path.write_text("# One heading\n\n" + ("word " * 200 + "\n\n") * 40, encoding="utf-8")


def _wide_csv(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "body"])
        for i in range(60):
            w.writerow([i, "cell " * 900])


def _one_line_json(path: Path) -> None:
    path.write_text(
        '{"posts": [' + ", ".join('{"t": "%s"}' % ("x" * 400) for _ in range(300)) + "]}"
    )


@pytest.mark.parametrize(
    ("name", "make"),
    [("notes.md", _long_markdown), ("rows.csv", _wide_csv), ("dump.json", _one_line_json)],
)
def test_a_real_oversized_file_of_each_text_kind_comes_out_bounded(
    name: str, make: object, tmp_path: Path
) -> None:
    """The two follow-ups the reviewer named (a capless markdown section, a CSV
    of enormous rows) and a one-line dump, through their real extractors."""
    f = tmp_path / name
    make(f)  # type: ignore[operator]

    chunks = list(extract(f))

    assert chunks, name
    assert all(len(c.body) <= MAX_CHUNK_CHARS for c in chunks), max(len(c.body) for c in chunks)
    assert [c.chunk_seq for c in chunks] == list(range(len(chunks)))
