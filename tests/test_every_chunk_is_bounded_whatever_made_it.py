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
    """Nine-character words: 8,000 is not a multiple of 9, so a hard cut lands
    mid-word and shows up as a fragment at a piece boundary."""
    pieces = list(bounded([_chunk([Block("p", "abcdefgh " * 3_000)])]))

    assert len(pieces) > 1
    assert all(len(p.body) <= MAX_CHUNK_CHARS for p in pieces)
    assert all(w == "abcdefgh" for p in pieces for w in p.body.split()), "a term was halved"


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


def test_the_source_is_sliced_by_the_spans_the_extractor_stamped() -> None:
    """The preview prefers verbatim markdown; a piece carries exactly the lines
    behind its own blocks. Two paragraphs open identically, which is what broke
    locating them by their text."""
    paras = ["**Note:** " + "text " * 700 for _ in range(4)]  # 3.5k each, two per piece
    source = "\n\n".join(paras) + "\n"
    blocks = [Block("p", t.replace("**", ""), span=(i * 2, i * 2 + 1)) for i, t in enumerate(paras)]

    pieces = list(bounded([_chunk(blocks, body_md=source)]))

    assert len(pieces) == 2
    assert pieces[0].body_md == "\n".join(source.splitlines()[0:3])
    assert pieces[1].body_md == "\n".join(source.splitlines()[4:7])


def test_a_run_with_any_unspanned_block_carries_no_source() -> None:
    """Exact or empty. A split single block has no span (a textured PDF page is
    one block), and a kind with no source map stamps none."""
    single = [Block("p", "rendered " * 1_100)]  # over the budget: split by text
    unspanned = [Block("p", "a " * 3_000), Block("p", "b " * 3_000)]

    from_single = list(bounded([_chunk(single, body_md="whole page markdown\n")]))
    from_unspanned = list(bounded([_chunk(unspanned, body_md="two paras\n")]))

    assert len(from_single) > 1
    assert all(p.body_md == "" for p in from_single)
    assert len(from_unspanned) > 1
    assert all(p.body_md == "" for p in from_unspanned)


def test_a_fence_at_a_cut_keeps_both_fence_lines(tmp_path: Path) -> None:
    """Through the real extractor: a piece that starts or ends with a fenced
    block keeps its fences, because the fence token's span covers them."""
    code = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(600)) + "\n```"
    body = "# H\n\n" + "prose " * 1_300 + "\n\n" + code + "\n\n" + "after " * 200 + "\n"
    f = tmp_path / "fence.md"
    f.write_text(body, encoding="utf-8")

    chunks = list(extract(f))

    fenced = [c for c in chunks if "x1 = 1" in c.body]
    assert fenced, [c.body[:30] for c in chunks]
    assert fenced[0].body_md.count("```") == 2, fenced[0].body_md[:60]


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


def test_every_markdown_block_span_covers_its_own_text(fixtures_dir: Path, tmp_path: Path) -> None:
    """The slice is only as good as the spans. Over every markdown fixture, plus
    one file carrying every block kind, every block that has a span finds its
    first word inside the lines the span names in the chunk's verbatim source."""
    rich = tmp_path / "rich.md"
    rich.write_text(
        "# Title\n\nOpening **bold** para.\n\n## Second\n\n"
        "- item one\n- item two\n\n> quoted words here\n\n"
        "```py\nx = 1\n```\n\n    indented code\n\n"
        "Closing para with *emphasis* and `code`.\n\n### Third\n\nLast words.\n",
        encoding="utf-8",
    )
    checked = 0
    for f in [*sorted(fixtures_dir.rglob("*.md")), rich]:
        for chunk in extract(f):
            lines = chunk.body_md.splitlines()
            for block in chunk.body_struct:
                if block.span is None or not block.text.split():
                    continue
                spanned = "\n".join(lines[block.span[0] : block.span[1]])
                first_word = block.text.split()[0].strip("*_`#>-")
                assert first_word in spanned, (f.name, block.kind, block.text[:40], spanned[:60])
                checked += 1
    assert checked > 25, checked
