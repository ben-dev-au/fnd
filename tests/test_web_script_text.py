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
