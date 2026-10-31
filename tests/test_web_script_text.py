"""HTML pages that build their body from inline script data."""

from __future__ import annotations

from pathlib import Path

from fnd.extract import extract
from fnd.extract.base import Chunk

ANCHOR = "blue penguin sandwich"

_PAGE = """<html><head><title>Retry guide</title></head><body>
<h1>Retry guide</h1>
<section id="backoff"><h2>Backoff</h2><div class="flow" data-flow="backoff"></div></section>
<section id="jitter"><h2>Jitter</h2><div class="flow" data-flow="jitter"></div></section>
<script>
{script}
</script></body></html>"""


def _chunks(tmp_path: Path, script: str) -> list[Chunk]:
    p = tmp_path / "guide.html"
    p.write_text(_PAGE.format(script=script), encoding="utf-8")
    return list(extract(p))


def _body_under(chunks: list[Chunk], heading: str) -> str:
    return "\n".join(c.body for c in chunks if c.heading_path.endswith(heading))


_FLOWS = f"""
var FLOWS = {{}};
FLOWS.backoff = {{
  code: ['client.retry(attempts=5)'],
  steps: [
    {{ line: 0, say: "Each retry waits twice as long as the <code>previous</code> one: {ANCHOR}.",
      views: [{{ type: "out", cmd: "client.retry()", out: "waited 4 seconds" }}] }}
  ]
}};
FLOWS.jitter = {{
  steps: [{{ say: 'Random jitter spreads the retries so clients do not stampede together.' }}]
}};
"""


def test_script_narration_lands_under_the_section_that_owns_it(tmp_path: Path) -> None:
    chunks = _chunks(tmp_path, _FLOWS)
    assert ANCHOR in _body_under(chunks, "Backoff")
    assert "stampede" in _body_under(chunks, "Jitter")
    assert "stampede" not in _body_under(chunks, "Backoff")


def test_inline_markup_in_script_strings_becomes_markdown(tmp_path: Path) -> None:
    md = "\n".join(c.body_md for c in _chunks(tmp_path, _FLOWS))
    assert "`previous`" in md


def test_short_labels_and_template_fragments_are_not_indexed(tmp_path: Path) -> None:
    script = (
        _FLOWS
        + """
function row(c) { return '<div class="chip a k' + c + '" style="--i:' + c + '">'; }
document.querySelector("section .flow").classList.add("is-ready now");
var tag = "kind";
"""
    )
    body = "\n".join(c.body for c in _chunks(tmp_path, script))
    assert "chip a k" not in body
    assert "is-ready" not in body
    assert "kind" not in body


def test_titles_inside_render_functions_are_kept_but_data_is_not_repeated(tmp_path: Path) -> None:
    script = """var VIEWS = {
      hint: "Remember to cap the maximum delay at one minute.",
      render: function () { return section('x', 'Timeline of key dates'); }
    };"""
    body = _body_under(_chunks(tmp_path, script), "Script text")
    assert "Timeline of key dates" in body
    assert body.count("cap the maximum delay") == 1


def test_call_shaped_strings_outside_data_objects_are_indexed_on_a_data_page(
    tmp_path: Path,
) -> None:
    script = (
        "var DATA = { note: 'Retries wait longer each time they run.' };"
        "show('client.retry_after()'); show('jitter');"
    )
    body = _body_under(_chunks(tmp_path, script), "Script text")
    assert "client.retry_after()" in body
    assert "jitter" not in body


def test_scripting_calls_on_an_ordinary_page_are_not_indexed(tmp_path: Path) -> None:
    script = "setTimeout(function () { window.close(); }, 1); show('window.close()');"
    assert _body_under(_chunks(tmp_path, script), "Script text") == ""


def test_escaped_quotes_are_unescaped(tmp_path: Path) -> None:
    script = (
        'FLOWS = {}; FLOWS.jitter = { say: "Call option(\\"header\\", True) before reading." };'
    )
    assert 'option("header", True)' in _body_under(_chunks(tmp_path, script), "Jitter")


def test_repeated_strings_are_indexed_once(tmp_path: Path) -> None:
    script = (
        "FLOWS = {}; FLOWS.backoff = {"
        ' a: "Double the delay after every failed attempt.",'
        ' b: "Double the delay after every failed attempt." };'
    )
    body = _body_under(_chunks(tmp_path, script), "Backoff")
    assert body.count("Double the delay") == 1


def test_script_text_without_a_matching_node_is_kept_under_its_own_heading(
    tmp_path: Path,
) -> None:
    script = 'var NOTES = { hint: "Remember to cap the maximum delay at one minute." };'
    chunks = _chunks(tmp_path, script)
    assert "cap the maximum delay" in _body_under(chunks, "Script text")


def test_external_scripts_and_pages_without_script_data_are_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "plain.html"
    p.write_text(
        '<html><body><h1>Doc</h1><p>Plain prose here.</p><script src="app.js"></script></body></html>',
        encoding="utf-8",
    )
    chunks = list(extract(p))
    assert [c.heading_path for c in chunks] == ["Doc"]


def _md(chunks: list[Chunk]) -> str:
    return "\n\n".join(c.body_md for c in chunks)


def test_decimal_negative_and_exponent_numbers_are_data(tmp_path: Path) -> None:
    script = "var R = [{ name: 'Profit', delta: -3.5, ratio: 0.125, big: 1e5 }, { name: 'Loss', delta: 2 }];"
    md = _md(_chunks(tmp_path, script))
    assert "| Profit | -3.5 | 0.125 | 100000.0 |" in md


def test_decimals_in_a_json_island_are_data(tmp_path: Path) -> None:
    p = tmp_path / "page.html"
    p.write_text(
        '<html><body><h1>Doc</h1><script type="application/json">'
        '[{"city": "Harbour", "lat": -33.86}, {"city": "Quay", "lat": 12.5}]'
        "</script></body></html>",
        encoding="utf-8",
    )
    md = "\n\n".join(c.body_md for c in extract(p))
    assert "| Harbour | -33.86 |" in md


def test_a_commented_out_assignment_does_not_hide_the_data_after_it(tmp_path: Path) -> None:
    script = (
        "// cfg.old = [retired]\n"
        "var DATA = { items: ['Retries wait longer every time.', 'Jitter spreads the retries.'] };"
    )
    assert "- Retries wait longer every time." in _md(_chunks(tmp_path, script))  # as data


def test_an_assignment_inside_a_string_is_not_data(tmp_path: Path) -> None:
    from fnd.extract._js_data import parse_assignments

    found = parse_assignments("var s = 'a.b = [1]'; var DATA = { note: 'x' };")
    assert [(a.base, a.name) for a in found] == [("", "DATA")]


def test_a_bare_global_assignment_is_data(tmp_path: Path) -> None:
    script = "DATA = { items: ['First point about the retries here.', 'Second point about jitter here.'] };"
    md = _md(_chunks(tmp_path, script))
    assert "- First point about the retries here." in md


def test_an_empty_data_attribute_does_not_claim_the_loose_prose(tmp_path: Path) -> None:
    p = tmp_path / "page.html"
    p.write_text(
        "<html><body><h1>Doc</h1><h2>Unrelated</h2><div data-v-7ba5bd90=''>Widget.</div>"
        "<script>show('A loose sentence the page shows at runtime.');</script></body></html>",
        encoding="utf-8",
    )
    under = {c.heading_path: c.body for c in extract(p)}
    assert "A loose sentence" not in under["Doc > Unrelated"]
    assert "A loose sentence" in under["Doc > Script text"]


def test_a_json_island_is_placed_in_the_section_where_it_sits(tmp_path: Path) -> None:
    island = '{"items": ["Opinion holder is who expresses it.", "Target is what it is about."]}'
    p = tmp_path / "page.html"
    p.write_text(
        "<html><body><h1>Doc</h1>"
        f'<h2>Parts</h2><div class="quiz"><script type="application/json">{island}</script></div>'
        "<h2>Later</h2><p>Unrelated section text.</p></body></html>",
        encoding="utf-8",
    )
    under = {c.heading_path: c.body for c in extract(p)}
    assert "Opinion holder is who expresses it." in under["Doc > Parts"]
    assert "Doc > Script text" not in under


def test_a_large_island_gets_sections_below_the_one_it_sits_in(tmp_path: Path) -> None:
    notes = ", ".join(
        f'"Harbour note number {i} about the berths and the tides."' for i in range(80)
    )
    marks = notes.replace("Harbour note", "Pier marker")
    island = f'{{"limits": [{notes}], "markers": [{marks}]}}'
    p = tmp_path / "page.html"
    p.write_text(
        f'<html><body><h1>Doc</h1><h2>Data</h2><div><script type="application/json">{island}'
        "</script></div></body></html>",
        encoding="utf-8",
    )
    paths = [c.heading_path for c in extract(p)]
    assert "Doc > Data > Limits" in paths
    assert "Doc > Data > Markers" in paths


def test_literals_inside_functions_that_do_not_run_at_load_are_marked_local() -> None:
    from fnd.extract._js_data import parse_assignments

    src = """
    var TOP = [1];
    if (ready) { var BLOCK = [2]; }
    (function () { var IIFE = [3]; })();
    (() => { var ARROW_IIFE = [4]; })();
    !function () { var BANG = [5]; }();
    function draw() { var DRAW = [6]; }
    var cb = function () { var ASSIGNED = [7]; };
    items.forEach(function (x) { var CALLBACK = [8]; });
    items.map((x) => { var ARROW_CB = [9]; });
    class View { render() { var METHOD = [10]; } }
    (function () { function inner() { var NESTED = [11]; } })();
    """
    local = {a.name: a.local for a in parse_assignments(src) if a.name.isupper()}
    assert local == {
        **dict.fromkeys(("TOP", "BLOCK", "IIFE", "ARROW_IIFE", "BANG"), False),
        **dict.fromkeys(("DRAW", "ASSIGNED", "CALLBACK", "ARROW_CB", "METHOD", "NESTED"), True),
    }


def test_a_lead_in_dropped_by_the_budget_does_not_take_earlier_content() -> None:
    from fnd.extract._script_text import _Renderer  # pyright: ignore[reportPrivateUsage]

    first = "A first paragraph that fits inside the budget."
    renderer = _Renderer({}, chars=len(first))  # no room left for the "B" lead-in
    renderer.render({"a": first, "b": ["Another sentence that will not fit at all."]})
    assert [b.text for b in renderer.blocks] == [first]
