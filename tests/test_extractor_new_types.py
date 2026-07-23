"""Unit tests for the file-type extractors added in the broad-support effort:
epub, web (html), code, data (csv/json), notebook (ipynb) and odf (odt/odp/ods).

Inputs are built in ``tmp_path`` (no committed binaries) and driven through the
real ``extract()`` dispatch, so these also exercise the registry wiring.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from fnd.extract import ExtractError, extract

ANCHOR = "blue penguin sandwich"


def _kinds(chunks) -> set[str]:
    return {c.kind for c in chunks}


def _anchor_chunk(chunks):
    return next(c for c in chunks if ANCHOR in c.body)


# ── code ──────────────────────────────────────────────────────────────────
def test_code_python(tmp_path: Path) -> None:
    p = tmp_path / "app.py"
    p.write_text(f"def greet():\n    # the {ANCHOR} lives here\n    return 1\n", encoding="utf-8")
    chunks = list(extract(p))
    assert _kinds(chunks) == {"python"}
    c = _anchor_chunk(chunks)
    assert c.body_md.startswith("```python")
    assert c.line == 1
    assert ANCHOR in c.body


def test_code_cpp_distinct_kind(tmp_path: Path) -> None:
    p = tmp_path / "main.cpp"
    p.write_text(f"// {ANCHOR}\nint main() {{ return 0; }}\n", encoding="utf-8")
    chunks = list(extract(p))
    assert _kinds(chunks) == {"cpp"}
    assert chunks[0].body_md.startswith("```cpp")


# ── data ──────────────────────────────────────────────────────────────────
def test_data_csv_renders_table(tmp_path: Path) -> None:
    p = tmp_path / "d.csv"
    p.write_text(f"name,role\nAnn,the {ANCHOR}\nBob,builder\n", encoding="utf-8")
    chunks = list(extract(p))
    assert _kinds(chunks) == {"csv"}
    c = _anchor_chunk(chunks)
    assert "| name | role |" in c.body_md
    assert "|------|" in c.body_md


def test_data_json_fenced(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text(f'{{"note": "the {ANCHOR}", "n": 42}}\n', encoding="utf-8")
    chunks = list(extract(p))
    assert _kinds(chunks) == {"json"}
    assert chunks[0].body_md.startswith("```json")


# ── notebook ──────────────────────────────────────────────────────────────
def test_notebook_cells(tmp_path: Path) -> None:
    p = tmp_path / "nb.ipynb"
    p.write_text(
        '{"cells":['
        f'{{"cell_type":"markdown","source":["# Analysis\\n","The {ANCHOR}."]}},'
        '{"cell_type":"code","source":["print(1+1)"],"outputs":[{"output_type":"stream","text":["2\\n"]}]}'
        '],"metadata":{"kernelspec":{"language":"python"}},"nbformat":4,"nbformat_minor":5}',
        encoding="utf-8",
    )
    chunks = list(extract(p))
    assert _kinds(chunks) == {"ipynb"}
    assert len(chunks) == 2
    assert _anchor_chunk(chunks).heading_path == "Analysis"
    code_chunk = chunks[1]
    assert code_chunk.body_md.startswith("```python")
    assert "2" in code_chunk.body  # captured stream output


def test_notebook_invalid_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.ipynb"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ExtractError):
        list(extract(p))


# ── web ───────────────────────────────────────────────────────────────────
def test_web_html_structure(tmp_path: Path) -> None:
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><head><title>Page</title></head><body><h1>Doc</h1>"
        f"<p>The {ANCHOR} on the web.</p><ul><li>a</li><li>b</li></ul></body></html>",
        encoding="utf-8",
    )
    chunks = list(extract(p))
    assert _kinds(chunks) == {"html"}
    c = _anchor_chunk(chunks)
    assert c.title == "Page"
    assert c.heading_path == "Doc"
    assert c.body_md.startswith("# Doc")


# ── epub ──────────────────────────────────────────────────────────────────
def _build_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Test Book</dc:title><dc:creator>Jane Author</dc:creator></metadata>"
            '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>',
        )
        z.writestr(
            "OEBPS/ch1.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter One</h1>'
            f"<p>The {ANCHOR} appeared.</p></body></html>",
        )
        z.writestr(
            "OEBPS/ch2.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter Two</h1>'
            "<p>Nothing here.</p></body></html>",
        )


def test_epub_spine_and_metadata(tmp_path: Path) -> None:
    p = tmp_path / "book.epub"
    _build_epub(p)
    chunks = list(extract(p))
    assert _kinds(chunks) == {"epub"}
    assert len(chunks) == 2  # one per chapter heading
    assert chunks[0].title == "Test Book"
    assert chunks[0].author == "Jane Author"
    assert [c.chunk_seq for c in chunks] == [0, 1]  # continuous across the book
    assert _anchor_chunk(chunks).heading_path == "Chapter One"


def test_epub_bad_zip_raises(tmp_path: Path) -> None:
    p = tmp_path / "broken.epub"
    p.write_bytes(b"not a zip at all")
    with pytest.raises(ExtractError):
        list(extract(p))


# ── odf ───────────────────────────────────────────────────────────────────
_NS = (
    ' xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
    ' xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
)


def _odf(path: Path, body: str) -> None:
    content = f'<?xml version="1.0"?><office:document-content{_NS}><office:body>{body}</office:body></office:document-content>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.xml", content)


def test_odt_headings(tmp_path: Path) -> None:
    p = tmp_path / "doc.odt"
    _odf(
        p,
        '<office:text><text:h text:outline-level="1">Intro</text:h>'
        f"<text:p>The {ANCHOR} here.</text:p>"
        "<text:list><text:list-item><text:p>one</text:p></text:list-item></text:list></office:text>",
    )
    chunks = list(extract(p))
    assert _kinds(chunks) == {"odt"}
    c = _anchor_chunk(chunks)
    assert c.body_md.startswith("# Intro")
    assert "- one" in c.body_md


def test_odp_slide(tmp_path: Path) -> None:
    p = tmp_path / "deck.odp"
    _odf(
        p,
        '<office:presentation><draw:page draw:name="Title"><draw:frame><draw:text-box>'
        f"<text:p>The {ANCHOR} on a slide.</text:p></draw:text-box></draw:frame></draw:page>"
        "</office:presentation>",
    )
    chunks = list(extract(p))
    assert _kinds(chunks) == {"odp"}
    assert chunks[0].slide == 1


def test_ods_table_and_repeat_cap(tmp_path: Path) -> None:
    p = tmp_path / "sheet.ods"
    _odf(
        p,
        '<office:spreadsheet><table:table table:name="Sheet1">'
        "<table:table-row><table:table-cell><text:p>Name</text:p></table:table-cell>"
        "<table:table-cell><text:p>Note</text:p></table:table-cell></table:table-row>"
        "<table:table-row><table:table-cell><text:p>Ann</text:p></table:table-cell>"
        f"<table:table-cell><text:p>the {ANCHOR}</text:p></table:table-cell></table:table-row>"
        '<table:table-cell table:number-columns-repeated="1000"/></table:table></office:spreadsheet>',
    )
    chunks = list(extract(p))
    assert _kinds(chunks) == {"ods"}
    c = chunks[0]
    assert "| Name | Note |" in c.body_md
    assert ANCHOR in c.body
    # The 1000-repeat empty trailing cell must not explode the grid width.
    assert c.body_md.count("|") < 30
