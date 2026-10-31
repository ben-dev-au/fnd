"""A hostile or huge page script costs time and output in proportion to its size, never more."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fnd.extract import extract
from fnd.extract._js_data import parse_assignments


@pytest.fixture(autouse=True)
def _static_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FND_HTML_EVAL", "0")


def _extract(tmp_path: Path, script: str) -> list[str]:
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><p>Ordinary page text stays searchable.</p>"
        f"<script>{script}</script></body></html>",
        encoding="utf-8",
    )
    return [c.body_md for c in extract(p)]


def test_shared_references_do_not_fan_out_exponentially(tmp_path: Path) -> None:
    lines = ['var A0 = ["four plain words here"];']
    lines += [f"var A{i} = [A{i - 1}, A{i - 1}];" for i in range(1, 40)]
    started = time.monotonic()
    chunks = _extract(tmp_path, "\n".join(lines))
    assert time.monotonic() - started < 5
    assert "Ordinary page text stays searchable." in "\n".join(chunks)


def test_a_long_reference_chain_keeps_the_pages_own_text(tmp_path: Path) -> None:
    lines = ['var A0 = ["a chain of four plain words"];']
    lines += [f"var A{i} = [A{i - 1}];" for i in range(1, 1500)]
    assert "Ordinary page text stays searchable." in "\n".join(_extract(tmp_path, "\n".join(lines)))


def test_a_shared_big_string_cannot_multiply_into_a_huge_output(tmp_path: Path) -> None:
    big = "word " * 100_000
    refs = ", ".join(["S"] * 3000)
    chunks = _extract(tmp_path, f'var S = "{big}"; var A = [{refs}];')
    assert sum(len(c) for c in chunks) < 20_000_000


def test_an_unterminated_comment_is_read_in_linear_time() -> None:
    src = "/* open " * 40_000
    started = time.monotonic()
    parse_assignments(src)
    assert time.monotonic() - started < 1.5


def test_a_wide_shared_reference_graph_is_checked_in_linear_time(tmp_path: Path) -> None:
    lines = ['var A0 = ["four plain words here"];']
    lines += [f"var A{i} = [{', '.join([f'A{i - 1}'] * 8)}];" for i in range(1, 10)]
    started = time.monotonic()
    chunks = _extract(tmp_path, "\n".join(lines))
    assert time.monotonic() - started < 5
    assert "Ordinary page text stays searchable." in "\n".join(chunks)


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_unterminated_string_literals_are_read_in_linear_time(quote: str) -> None:
    started = time.monotonic()
    parse_assignments(quote + ("\\" + quote) * 20_000)
    assert time.monotonic() - started < 1.5


def test_random_script_text_never_raises_inside_the_renderer() -> None:
    """``script_text`` swallows errors by design, so call the real work to see them."""
    import random

    from fnd.extract._html import parse
    from fnd.extract._script_text import _script_blocks  # pyright: ignore[reportPrivateUsage]

    pieces = [
        "var A = {",
        "}",
        "[",
        "]",
        '"x y z w"',
        "'",
        "`",
        "\\",
        "/*",
        "*/",
        "//",
        ",",
        ":",
        ";",
    ]
    pieces += [
        "\\ud83d",
        "\\ude80",
        "\\u001b[1m",
        "FLOWS.a = ",
        "say: ",
        "steps: [",
        "C(1)",
        "null",
        "<b>",
    ]
    rng = random.Random(7)
    for _ in range(400):
        src = " ".join(rng.choice(pieces) for _ in range(rng.randint(1, 60)))
        root = parse(f"<html><body><script>{src}</script></body></html>".encode())
        for blocks in _script_blocks(root).values():
            for b in blocks:
                (b.text + "".join(map("".join, b.rows))).encode("utf-8")


def _timed(tmp_path: Path, script: str, seconds: float = 5) -> list[str]:
    started = time.monotonic()
    chunks = _extract(tmp_path, script)
    assert time.monotonic() - started < seconds
    assert sum(map(len, chunks)) < 6_000_000
    return chunks


def test_a_long_run_of_word_characters_does_not_slow_assignment_search(tmp_path: Path) -> None:
    _timed(tmp_path, 'var d = { bdata: "' + "A" * 60_000 + '" };', 3)


def test_a_shared_big_string_in_steps_lines_and_chips_is_bounded(tmp_path: Path) -> None:
    big = "x-" * 200_000
    refs = ", ".join(["S"] * 1200)
    script = (
        f'var S = "{big}"; var F = {{}}; F.d = {{ code: [S], steps: ['
        f"{{ say: 'See the listing run.', line: [{', '.join(['0'] * 1200)}],"
        f" views: [{{ type: 'term', lines: [{', '.join(['{ o: S }'] * 1200)}] }},"
        f" {{ type: 'rows', items: [{refs}] }}] }}] }};"
    )
    _timed(tmp_path, script)


def test_a_table_header_wider_than_its_rows_cannot_pad_without_limit(tmp_path: Path) -> None:
    cols = ", ".join(['"c"'] * 10_000)
    rows = ", ".join(["E"] * 5_000)
    script = (
        f"var E = []; var F = {{}}; F.d = {{ steps: [{{ say: 'A wide table.',"
        f" views: [{{ type: 'table', cols: [{cols}], rows: [{rows}] }}] }}] }};"
    )
    _timed(tmp_path, script)


def test_many_keys_sharing_one_nested_list_in_a_record_are_bounded(tmp_path: Path) -> None:
    lines = ['var A0 = ["w"];', f"var A1 = [{', '.join(['A0'] * 1000)}];"]
    lines.append(f"var A2 = [{', '.join(['A1'] * 90)}];")
    keys = ", ".join(f"k{i}: A2" for i in range(1000))
    lines.append(f'var T = [{{ {keys} }}, {{ k0: "second row" }}];')
    _timed(tmp_path, "\n".join(lines))


def test_a_nested_list_of_strings_in_a_record_is_checked_in_linear_time(tmp_path: Path) -> None:
    words = ", ".join(f'"w{i}"' for i in range(60_000))
    _timed(tmp_path, f"var DATA = [{{ a: 'x', v: [[{words}]], d: {{ n: 1 }} }}, {{ a: 'y' }}];")


def test_a_shared_string_visited_again_and_again_is_bounded(tmp_path: Path) -> None:
    refs = ", ".join(["S"] * 800)
    _timed(tmp_path, f'var S = "{"x-" * 100_000}"; var A = [{refs}];')


def test_a_heading_that_starts_with_a_comment_keeps_the_page(tmp_path: Path) -> None:
    p = tmp_path / "page.html"
    p.write_text(
        "<html><body><h2><!-- anchor -->Installation guide</h2><p>Body text.</p></body></html>",
        encoding="utf-8",
    )
    assert "Installation guide" in "\n".join(c.body for c in extract(p))


_DOCS = [
    ("application/json", '{"x": "<html><head><title>Demo</title></head></html>"}'),
    (
        "text/javascript",
        "var F = {}; F.d = { steps: [{ say: 'A code view.', views: "
        "[{ type: 'code', body: '<html><head></head></html>' }] }] };",
    ),
    ("text/javascript", "var R = [{ a: '<html>', b: 'one' }, { a: '<!doctype html>', b: 'two' }];"),
]


@pytest.mark.parametrize(("kind", "script"), _DOCS)
def test_a_whole_document_inside_script_data_cannot_fail_the_page(
    tmp_path: Path, kind: str, script: str
) -> None:
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><p>Ordinary page text stays searchable.</p>"
        f'<script type="{kind}">{script}</script></body></html>',
        encoding="utf-8",
    )
    assert "Ordinary page text stays searchable." in "\n".join(c.body for c in extract(p))


def test_thousands_of_out_views_in_one_step_are_linear(tmp_path: Path) -> None:
    views = ",".join(f"{{type:'out',cmd:'c{i}'}}" for i in range(65_000))
    _timed(tmp_path, f"var F = {{ steps: [{{ say: 'Many outputs.', views: [{views}] }}] }};", 8)


def test_a_shared_list_of_line_numbers_in_every_step_is_bounded() -> None:
    """The renderer is fed directly: the parser's own cap would otherwise end this first."""
    from fnd.extract._script_text import _Renderer  # pyright: ignore[reportPrivateUsage]

    step = {"say": "<", "line": [5] * 80_000}  # out of range: no text reaches the character budget
    renderer = _Renderer({})
    started = time.monotonic()
    renderer.render({"code": ["x"], "steps": [step] * 8_000})
    assert time.monotonic() - started < 3


def test_a_covered_nested_field_scan_is_charged_against_the_budget(tmp_path: Path) -> None:
    z = ", ".join(["{ q: 'zq' }"] * 200)
    fields = ", ".join(f"n{i}: Z" for i in range(800))
    script = f"var Z = [{z}]; var R = [{{ a: '{'w' * 1_000_000} zq', b: 'x', {fields} }}, {{ a: 'second' }}];"
    _timed(tmp_path, script, 8)


def test_pathological_cell_text_does_not_stall_the_cell_rules(tmp_path: Path) -> None:
    link_runs = "[" * 100_000
    breaks = "<b>x</b>" + " " * 100_000 + "<br>"
    script = f"var R = [{{ a: '{link_runs}', b: 'one' }}, {{ a: '{breaks}', b: 'two' }}];"
    _timed(tmp_path, script, 4)


def test_nested_tables_are_rendered_once_not_once_per_level(tmp_path: Path) -> None:
    depth, filler = 62, "<i>a</i>" * 40_000
    html = "<table><tr><td>" * depth + filler + "</td></tr></table>" * depth
    p = tmp_path / "page.html"
    p.write_text(f"<html><body>{html}</body></html>", encoding="utf-8")
    started = time.monotonic()
    assert "aaaa" in "\n".join(c.body for c in extract(p))
    assert time.monotonic() - started < 4


def test_marking_blocks_keeps_deeply_nested_inline_markup_linear(tmp_path: Path) -> None:
    html = "<div>" + "<span>" * 250 + "<br>" * 70_000 + "<p>end</p>" + "</span>" * 250 + "</div>"
    p = tmp_path / "page.html"
    p.write_text(f"<html><body>{html}</body></html>", encoding="utf-8")
    started = time.monotonic()
    assert "end" in "\n".join(c.body for c in extract(p))
    assert time.monotonic() - started < 5


def test_a_fragment_nested_as_deep_as_the_page_still_indexes_the_page(tmp_path: Path) -> None:
    flow = '"body": "' + "<b>" * 250 + "inner fragment words here" + "</b>" * 250 + '"'
    script = (
        'FLOWS.fs = {"steps":[{"say":"This step narrates the flow here.",'
        f'"views":[{{"type":"flow",{flow}}}]}}]}};'
    )
    page = "<div>" * 250 + "<div id='fs'>x</div>" + "</div>" * 250
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><p>Ordinary page text stays searchable.</p>{page}"
        f"<script>{script}</script></body></html>",
        encoding="utf-8",
    )
    assert "Ordinary page text stays searchable." in "\n".join(c.body for c in extract(p))


def test_a_large_honest_table_is_kept_whole(tmp_path: Path) -> None:
    rows = ",".join(f"{{a:'R{i}',b:'x',c:'y'}}" for i in range(80_000))
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><script>var DATA = [{rows}];</script></body></html>",
        encoding="utf-8",
    )
    started = time.monotonic()
    chunks = list(extract(p))
    assert time.monotonic() - started < 15
    assert "R79999" in "\n".join(c.body for c in chunks)


def test_a_forged_result_line_is_ignored() -> None:
    from fnd.extract import _js_eval

    assert _js_eval._read("@@m@@[1, 2, 3]", "@@m@@") == {}  # pyright: ignore[reportPrivateUsage]


def test_a_large_honest_data_page_is_not_quadratic(tmp_path: Path) -> None:
    notes = ", ".join(
        f'"Sentence number {i} says something distinct about harbour works."' for i in range(8000)
    )
    started = time.monotonic()
    _extract(tmp_path, f"var DATA = {{ notes: [{notes}] }};")
    assert time.monotonic() - started < 6
