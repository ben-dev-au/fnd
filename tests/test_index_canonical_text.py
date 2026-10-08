"""The index holds text in the same canonical form every query is put into."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from fnd.extract import ExtractError, extract
from fnd.index import build_index
from fnd.query import Searcher
from fnd.text_canon import canonical

NFD = "cafe\N{COMBINING ACUTE ACCENT}"
NFC = "caf\N{LATIN SMALL LETTER E WITH ACUTE}"


def _hits(index_dir: Path, query: str) -> set[str]:
    return {Path(h.path).name for h in Searcher(index_dir=index_dir).search(query, limit=20)}


# ── decode (finding 24) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (b"\xef\xbb\xbfhello", "hello"),
        ("hello".encode("utf-16"), "hello"),
        (NFC.encode("utf-8"), NFC),
        (b"caf\xe9", NFC),
        (b"a\r\nb", "a\nb"),
    ],
)
def test_bytes_decode_one_way(raw: bytes, text: str) -> None:
    from fnd.text_canon import decode

    assert decode(raw) == text


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("a\fb", "a b"),
        ("a\vb", "a b"),
        ("a\N{LINE SEPARATOR}b", "a b"),
        ("a\x85b", "a b"),
        ("a\rb", "a\nb"),
    ],
)
def test_canonical_keeps_the_line_count_an_editor_sees(raw: str, text: str) -> None:
    """Only CR and CRLF break a line in an editor and in markdown-it; the rest are spaces."""
    assert canonical(raw) == text


# ── index parity (finding 19) ────────────────────────────────────────


@pytest.fixture
def corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "nfd.md").write_text(f"# Menu\n\nThe {NFD} opens late.\n", encoding="utf-8")
    (docs / "zw.md").write_text(
        "# Login\n\nYour pass\N{ZERO WIDTH SPACE}word here.\n", encoding="utf-8"
    )
    (docs / "lig.md").write_text(
        "# Data\n\nThe \N{LATIN SMALL LIGATURE FI}lter stage.\n", encoding="utf-8"
    )
    (docs / "shy.md").write_text(
        "# Words\n\nA wonder\N{SOFT HYPHEN}ful sentence.\n", encoding="utf-8"
    )
    (docs / "bom.md").write_bytes(
        b"\xef\xbb\xbf---\ntags: [alpha]\nsecretkey: zebrafish\n---\n# Body\n\nvisible text\n"
    )
    (docs / "page.html").write_bytes(f"<html><body><p>The {NFC} menu</p></body></html>".encode())
    nb = {"cells": [{"cell_type": "markdown", "source": ["notebookword"]}], "metadata": {}}
    (docs / "nb.ipynb").write_bytes(b"\xef\xbb\xbf" + json.dumps(nb).encode())
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.parametrize(
    ("query", "name"),
    [
        (NFC, "nfd.md"),
        ("password", "zw.md"),
        ("filter", "lig.md"),
        ("wonderful", "shy.md"),
        ("notebookword", "nb.ipynb"),
    ],
)
def test_a_canonical_query_finds_text_written_any_way(corpus: Path, query: str, name: str) -> None:
    assert name in _hits(corpus, query)


def test_a_decomposed_file_name_is_indexed_composed_and_stored_as_is() -> None:
    from fnd.extract.base import Chunk
    from fnd.index import _doc_for_chunk  # pyright: ignore[reportPrivateUsage]
    from fnd.schema import F_PATH, F_PATH_TOKENS

    path = f"/notes/{NFD}.md"
    doc = _doc_for_chunk(
        Chunk(parent_id="p", path=path, mtime=0, kind="md", body="x"), memberships=()
    )
    fields = doc.to_dict()
    assert fields[F_PATH_TOKENS] == [f"/notes/{NFC}.md"]
    assert fields[F_PATH] == [path], "the stored path must stay the one that opens the file"


def test_utf8_html_without_a_charset_is_read_as_utf8(corpus: Path) -> None:
    assert "page.html" in _hits(corpus, NFC)


def test_a_bom_does_not_turn_frontmatter_into_body(corpus: Path) -> None:
    assert "bom.md" not in _hits(corpus, "zebrafish")
    assert "bom.md" in _hits(corpus, "visible")


# ── tags (finding 19) ────────────────────────────────────────────────


def test_a_decomposed_tag_is_the_composed_one() -> None:
    from fnd.tags import normalise_tag

    assert normalise_tag(NFD) == normalise_tag(NFC)


# ── line maps (finding 25) ───────────────────────────────────────────


@pytest.mark.parametrize("brk", ["\N{LINE SEPARATOR}", "\f", "\x85"])
def test_a_section_body_is_its_own_source_lines(tmp_path: Path, brk: str) -> None:
    doc = tmp_path / "a.md"
    doc.write_text(f"# A\nx{brk}y\n# B\nbeta line\n", encoding="utf-8")
    second = next(c for c in extract(doc) if c.heading_path.endswith("B"))
    assert second.body_md.lstrip().startswith("# B"), second.body_md


# ── file names (finding 6) ───────────────────────────────────────────


def test_a_file_name_that_is_not_utf8_is_a_skip_not_a_crash(tmp_path: Path) -> None:
    from fnd.display_text import display_line, terminal_line
    from fnd.extract.base import file_parent_id

    bad = tmp_path / "bad\udcffname.md"
    assert len(file_parent_id(bad)) == 40
    with pytest.raises(ExtractError) as err:
        list(extract(bad))
    for show in (display_line, terminal_line):
        show(str(err.value)).encode("utf-8")


def test_a_state_file_holds_a_name_that_is_not_utf8(tmp_path: Path) -> None:
    import tomllib

    from fnd.index_runner import IndexState, save_state

    state = IndexState(collection="c", started_at="now", total_files=1)
    state.current_file = "/x/bad" + chr(0xDCFF) + "name.md"
    save_state(tmp_path / "s.toml", state)
    saved = tomllib.loads((tmp_path / "s.toml").read_text(encoding="utf-8"))
    assert saved["state"]["current_file"].startswith("/x/bad")


# ── review: decode, speed, parity ────────────────────────────────────


def test_one_stray_byte_keeps_the_rest_utf8() -> None:
    from fnd.text_canon import decode

    kanji = "\N{CJK UNIFIED IDEOGRAPH-65E5}\N{CJK UNIFIED IDEOGRAPH-672C}"
    raw = f"{NFC} {kanji} ".encode() + b"\xff"
    assert decode(raw).startswith(f"{NFC} {kanji}")


def test_the_hidden_class_is_every_cc_and_cf_but_the_kept_ones() -> None:
    from fnd.text_canon import _HIDDEN  # pyright: ignore[reportPrivateUsage]

    kept = "\t\n\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}"
    want = {
        cp
        for cp in range(0x110000)
        if unicodedata.category(chr(cp)) in ("Cc", "Cf") and chr(cp) not in kept
    }
    got = {cp for cp in range(0x110000) if _HIDDEN.fullmatch(chr(cp))}
    assert got == want


@pytest.mark.parametrize("sep", ["\N{LINE SEPARATOR}", "\f", "\x85", "\v"])
def test_a_deep_link_line_counts_lines_as_an_editor_does(tmp_path: Path, sep: str) -> None:
    doc = tmp_path / "a.md"
    doc.write_text(f"# A\nx{sep}y\n# B\nbeta line\n", encoding="utf-8")
    second = next(c for c in extract(doc) if c.heading_path.endswith("B"))
    assert second.line == 3


@pytest.mark.parametrize(("literal", "value"), [(NFD, NFC), (NFC, NFD)])
def test_a_filter_literal_meets_a_value_spelt_the_other_way(literal: str, value: str) -> None:
    from fnd.filter_dsl import compile_filter

    assert compile_filter(f"Course == '{literal}'")({"Course": value})
    assert compile_filter(f"Course in ['{literal}']")({"Course": value})


@pytest.mark.parametrize("raw", [f"x\nThe {NFD} line\n".encode(), b"x\nThe caf\xe9 line\n"])
def test_the_opener_finds_the_match_line_the_index_saw(tmp_path: Path, raw: bytes) -> None:
    from fnd.apps import _resolve_match_line  # pyright: ignore[reportPrivateUsage]

    doc = tmp_path / "a.md"
    doc.write_bytes(raw)
    assert _resolve_match_line(doc, NFC, 1) == 2


def test_frontmatter_is_read_from_the_head_alone(tmp_path: Path) -> None:
    """A whole-file read would see the body's bad bytes and decode the head as cp1252."""
    from fnd.frontmatter import read_frontmatter_from_file

    doc = tmp_path / "a.md"
    head = f"---\nCourse: {NFC}\n---\n".encode()
    doc.write_bytes(head + b"body line\n" * 20_000 + b"\xff\n" * 50)
    assert read_frontmatter_from_file(doc) == {"Course": NFC}


def test_a_name_that_is_not_utf8_keeps_the_failure_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fnd.tui import failure_log

    log = tmp_path / "failures.toml"
    monkeypatch.setattr("fnd.tui.failure_log._log_path", lambda: log)
    failure_log.record_failure(collection="c", path="/x/good.pdf", reason="flat")
    failure_log.record_failure(collection="c", path="/x/bad" + chr(0xDCFF) + ".pdf", reason="r")
    assert "good.pdf" in log.read_text(encoding="utf-8")
    assert "bad" in log.read_text(encoding="utf-8")


def test_a_name_that_is_not_utf8_is_spelt_one_way_everywhere() -> None:
    from fnd.display_text import display_line, terminal_line
    from fnd.paths import storable

    name = "bad" + chr(0xDCFF) + ".md"
    assert display_line(name) == terminal_line(name) == storable(name) == "bad\\udcff.md"


# ── matching a decomposed folder name ────────────────────────────────


@pytest.fixture
def nfd_folder(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / NFD).mkdir(parents=True)
    (root / NFD / "a.md").write_text("x", encoding="utf-8")
    (root / "keep.md").write_text("x", encoding="utf-8")
    return root


def test_a_composed_exclude_prunes_a_decomposed_folder(nfd_folder: Path) -> None:
    from fnd.walk import walk

    walked = {p.name for p in walk(roots=[nfd_folder], excludes=[f"{NFC}/**"])}
    assert walked == {"keep.md"}


def test_a_composed_ignore_line_ignores_a_decomposed_folder(nfd_folder: Path) -> None:
    from fnd.walk import walk

    (nfd_folder / ".fndignore").write_text(f"{NFC}/\n", encoding="utf-8")
    walked = {p.name for p in walk(roots=[nfd_folder], ignore_names=(".fndignore",))}
    assert walked == {"keep.md"}


def test_a_filter_rule_answers_what_the_walk_would(nfd_folder: Path) -> None:
    from fnd.filter_dsl import compile_filter
    from fnd.globs import GlobSet

    rel = f"{NFD}/a.md"
    glob = f"{NFC}/**"
    assert GlobSet.parse([glob]).matches(rel)
    assert compile_filter(f"file.path ~~ '{glob}'")({"file.path": rel})
