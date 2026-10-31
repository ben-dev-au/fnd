"""HTML components keep their meaning in the preview: callouts, inline code, languages, diagrams."""

from __future__ import annotations

import time
from pathlib import Path

from fnd.extract import extract
from fnd.extract.base import Chunk


def _chunks(tmp_path: Path, html: str) -> list[Chunk]:
    p = tmp_path / "page.html"
    p.write_text(f"<html><body><h1>Doc</h1>{html}</body></html>", encoding="utf-8")
    return list(extract(p))


def _md(tmp_path: Path, html: str) -> str:
    return "\n\n".join(c.body_md for c in _chunks(tmp_path, html))


def _body(tmp_path: Path, html: str) -> str:
    return "\n".join(c.body for c in _chunks(tmp_path, html))


_WARN = '<div class="warn"><b>Big results</b>Every record travels to the driver.</div>'


def test_a_warning_box_becomes_an_obsidian_callout(tmp_path: Path) -> None:
    assert "> [!warning] Big results\n> Every record travels to the driver." in _md(tmp_path, _WARN)


def test_callout_markers_stay_out_of_the_indexed_text(tmp_path: Path) -> None:
    body = _body(tmp_path, _WARN)
    assert "Big results" in body
    assert "driver" in body
    assert "[!warning]" not in body


def test_a_leading_bold_title_is_spaced_from_its_text(tmp_path: Path) -> None:
    assert "**Heads up** Read this first." in _md(
        tmp_path, "<p><b>Heads up</b>Read this first.</p>"
    )


def test_table_cells_and_quotes_keep_inline_code(tmp_path: Path) -> None:
    md = _md(
        tmp_path,
        "<table><tr><th>Call</th></tr><tr><td><code>rdd.count()</code></td></tr></table>"
        "<blockquote>Use <code>take</code> first.</blockquote>",
    )
    assert "| `rdd.count()` |" in md
    assert "> Use `take` first." in md


def test_a_code_language_class_becomes_the_fence_language(tmp_path: Path) -> None:
    md = _md(tmp_path, '<pre><code class="language-python">x = 1</code></pre>')
    assert "```python\nx = 1\n```" in md


_PAGE_SCRIPT = """
var FLOWS = {};
FLOWS.demo = { code: ["ls /data", "rdd.count()"], steps: [
  { line: 0, kind: "Sh", say: "List the folder first.", views: [] },
  { line: 1, kind: "A", say: "Count the records in the RDD.", views: [] }] };
var KIND = { Sh: "Shell command", A: "Action: runs now" };
"""


def _flow(tmp_path: Path, script: str) -> str:
    return _md(tmp_path, f'<h2>Demo</h2><div data-flow="demo"></div><script>{script}</script>')


def test_shell_steps_get_a_bash_fence(tmp_path: Path) -> None:
    md = _flow(tmp_path, _PAGE_SCRIPT)
    assert "```bash\nls /data\n```" in md


def test_a_view_caption_with_no_subtitle_is_not_left_dangling(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.demo = { steps: [ { say: "Show the records so far.",
      views: [{ type: "rows", label: "women", items: [] }] } ] };"""
    md = _flow(tmp_path, script)
    assert "*women*" in md
    assert "· *" not in md


def test_chip_rows_render_as_inline_code(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.demo = { steps: [ { say: "Two records, one per line.",
      views: [{ type: "rows", label: "nums", sub: "RDD",
                items: [C("1", "p0"), C("2", "p1")] }] } ] };"""
    md = _flow(tmp_path, script)
    assert "*nums · RDD*" in md
    assert "`1` `2`" in md


def test_row_mappings_render_as_a_table_with_the_views_own_headings(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.demo = { steps: [ { say: "Each record is upper-cased.",
      views: [{ type: "map", inLabel: "raw", mid: "step", outLabel: "clean",
                rows: [{ a: C("ab"), b: C("AB"), why: "upper" }] }] } ] };"""
    md = _flow(tmp_path, script)
    assert "| raw | clean | step |" in md
    assert "| `ab` | `AB` | upper |" in md


def _view_md(tmp_path: Path, view: str) -> str:
    script = f"""FLOWS = {{}}; FLOWS.demo = {{ steps: [ {{ say: "Look at what the step did.",
      views: [{view}] }} ] }};"""
    return _flow(tmp_path, script)


def test_an_html_view_is_walked_as_a_document_not_dumped_as_code(tmp_path: Path) -> None:
    html = (
        '<div class=\\"gwrap\\"><table><thead><tr><th>x[2]</th><th>int(x[2]) &lt; 25</th></tr></thead>'
        "<tbody><tr><td>1</td><td>yes</td></tr></tbody></table></div>"
    )
    md = _view_md(tmp_path, f'{{ type: "html", html: "{html}" }}')
    assert "| x[2] | int(x[2]) < 25 |" in md
    assert "| 1 | yes |" in md
    assert "<table" not in md
    assert "&lt;" not in md


def test_an_inline_html_view_stays_one_line(tmp_path: Path) -> None:
    html = '<div class=\\"anat\\"><span>Row(</span><b>x[0]</b><span>)</span></div>'
    md = _view_md(tmp_path, f'{{ type: "html", html: "{html}" }}')
    assert "Row( **x[0]** )" in md
    assert "<" not in md.partition("**Step 1**")[2]


def test_a_true_flag_in_a_row_table_is_not_an_empty_cell(tmp_path: Path) -> None:
    md = _view_md(tmp_path, '{ type: "groups", groups: [{ key: C("k"), sealed: true }] }')
    assert "| `k` | yes |" in md


def test_a_takeaway_paragraph_is_emphasised(tmp_path: Path) -> None:
    md = _md(
        tmp_path, '<p class="key">Read only from the path you name.</p><p>Plain text here.</p>'
    )
    assert "**Read only from the path you name.**" in md
    assert "**Plain text here.**" not in md


def test_a_pipeline_of_plan_steps_renders_as_one_arrow_line(tmp_path: Path) -> None:
    md = _view_md(
        tmp_path, '{ type: "plan", steps: [{ name: "textFile" }, { name: "map split" }] }'
    )
    assert "`textFile` → `map split`" in md


def test_a_dropped_row_says_so(tmp_path: Path) -> None:
    md = _view_md(
        tmp_path,
        '{ type: "map", inLabel: "in", outLabel: "out", rows: [{ a: C("x"), b: null, why: "no" }] }',
    )
    assert "| `x` | dropped | no |" in md


def test_gate_results_name_the_rule_that_matched(tmp_path: Path) -> None:
    md = _view_md(
        tmp_path,
        '{ type: "gates", col: "x[2]", rules: ["a < 1", "else"], items: [{ v: "3", hit: 1, out: "B" }] }',
    )
    assert "| x[2] | first true rule | out |" in md
    assert "**col**" not in md
    assert "**rules**" not in md
    assert "| 3 | `else` | B |" in md


def test_partition_lists_render_one_line_per_partition(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.demo = { steps: [ { say: "The list is cut into pieces.",
      views: [{ type: "parts", label: "rdd", parts: [[C("1"), C("2")], [C("3")]] }] } ] };"""
    md = _flow(tmp_path, script)
    assert "- **Part 0** `1` `2`" in md
    assert "- **Part 1** `3`" in md


def test_minified_sibling_elements_keep_their_words_apart(tmp_path: Path) -> None:
    html = (
        "<nav><a href='/a'>Home</a><a href='/b'>About</a></nav>"
        "<dl><dt>Apple</dt><dd>A fruit</dd><dt>Brick</dt><dd>A block</dd></dl>"
        "<div><span>Price</span><span>$10</span></div>"
    )
    body = _body(tmp_path, html)
    for words in ("Home About", "Apple\nA fruit", "Brick\nA block", "Price $10"):
        assert words in body


def test_words_split_by_styling_stay_whole(tmp_path: Path) -> None:
    html = (
        '<p><code><span class="n">os</span><span class="o">.</span><span class="n">path</span></code></p>'
        "<p>$<span>10</span><span>.99</span> and <span>T</span><span>HE</span></p>"
        "<table><tr><td><span>4</span><span>2</span></td></tr></table>"
    )
    body = _body(tmp_path, html)
    assert "os.path" in body
    assert "$10.99" in body
    assert "THE" in body
    assert "42" in body


def test_markdown_and_index_text_space_a_sentence_boundary_alike(tmp_path: Path) -> None:
    html = "<p><em>Note.</em><strong>Important</strong> stuff</p>"
    assert "Note. Important stuff" in _body(tmp_path, html)
    assert "*Note.* **Important** stuff" in _md(tmp_path, html)


def test_inline_markup_after_a_block_in_a_list_item_stays_one_paragraph(tmp_path: Path) -> None:
    md = _md(
        tmp_path,
        '<ul><li>Run <pre>make</pre> then <code>check</code> the <a href="/log">log</a> output</li></ul>',
    )
    assert "then `check` the [log](/log) output" in md


def test_a_list_item_made_of_blocks_keeps_its_bullet(tmp_path: Path) -> None:
    md = _md(tmp_path, "<ul><li><div>Ada</div><div>Engineer</div></li></ul>")
    assert "- Ada" in md
    assert "Engineer" in md


def test_a_standalone_button_label_is_indexed(tmp_path: Path) -> None:
    html = '<button class="accordion">What is it?</button><div class="panel">An answer.</div>'
    assert "What is it?" in _body(tmp_path, html)


def test_table_cells_keep_a_bare_less_than_and_url_ampersands(tmp_path: Path) -> None:
    script = (
        "var DATA = { rows: [{ a: 'x<y and z', b: 'q' }, "
        "{ a: '[l](https://e.example/?a=1&copy=2)', b: 'r' }] };"
    )
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><script>{script}</script></body></html>", encoding="utf-8"
    )
    md = "\n".join(c.body_md for c in extract(p))
    assert "x<y and z" in md
    assert "a=1&copy=2" in md


def test_ansi_colour_codes_in_script_data_do_not_fail_the_page(tmp_path: Path) -> None:
    script = 'var NOTES = { hint: "The run printed \\u001b[32m<b>passed</b>\\u001b[0m for every test case." };'
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><p>Ordinary page text stays searchable.</p>"
        f"<script>{script}</script></body></html>",
        encoding="utf-8",
    )
    chunks = list(extract(p))
    text = "\n".join(c.body for c in chunks)
    md = "\n".join(c.body_md for c in chunks)
    assert "Ordinary page text stays searchable." in text
    assert "**passed**" in md
    assert "<b>" not in md
    assert "\x1b" not in text + md


def test_json_ld_metadata_is_not_indexed_as_page_content(tmp_path: Path) -> None:
    ld = '{"@type": "NewsArticle", "headline": "A headline about harbour works", "author": {"@type": "Person"}}'
    p = tmp_path / "page.html"
    p.write_text(
        f'<html><body><h1>Doc</h1><p>Body text of the article.</p><script type="application/ld+json">{ld}</script></body></html>',
        encoding="utf-8",
    )
    text = "\n".join(c.body for c in extract(p))
    assert "NewsArticle" not in text
    assert "Script text" not in text


def test_list_text_after_a_block_child_is_kept(tmp_path: Path) -> None:
    md = _md(
        tmp_path,
        "<ul><li>Run this <pre>make all</pre> then check the output</li>"
        "<li>Lead<ul><li>child</li></ul>after nested</li></ul>",
    )
    assert "then check the output" in md
    assert "after nested" in md


def test_a_button_inside_a_sentence_stays_in_the_text(tmp_path: Path) -> None:
    assert "Click Save to save." in _body(tmp_path, "<p>Click <button>Save</button> to save.</p>")


def test_a_lead_paragraph_with_inline_bold_is_not_re_wrapped(tmp_path: Path) -> None:
    assert "****" not in _md(tmp_path, '<p class="lead">Some <b>bold</b> words in a lead</p>')


def test_ordinary_class_names_do_not_become_callouts_or_headings(tmp_path: Path) -> None:
    md = _md(
        tmp_path,
        '<div class="post-info">Posted today by someone.</div>'
        '<div class="error-message">Something went wrong here.</div>'
        '<div class="part-number"><b>7</b><span>of 9</span></div>',
    )
    assert "[!" not in md
    assert "## 7" not in md


def test_a_dotted_number_split_by_a_tag_is_not_spaced(tmp_path: Path) -> None:
    md = _md(tmp_path, "<p>Version 1.<span>2</span> and example.<b>com</b> ok</p>")
    assert "Version 1.2" in md
    assert "example.**com**" in md


def test_glued_widgets_of_any_kind_keep_their_words_apart(tmp_path: Path) -> None:
    html = (
        "<div><strong>Alpha</strong><strong>Beta</strong></div>"
        "<div><code>foo</code><code>bar</code></div>"
        "<div><span>Status</span><em>Pending</em></div>"
        "<form><label>Comment</label><textarea>Hello</textarea></form>"
    )
    body = _body(tmp_path, html)
    for words in ("Alpha Beta", "foo bar", "Status Pending", "Comment Hello"):
        assert words in body


def test_a_list_item_with_thousands_of_children_is_linear(tmp_path: Path) -> None:
    links = "".join(f'<a href="/{i}">link{i}</a>' for i in range(8000))
    started = time.monotonic()
    body = _body(tmp_path, f"<ul><li>{links}</li></ul><p>{'<b>x</b>' * 20000}</p>")
    assert time.monotonic() - started < 3
    assert "link7999" in body


def test_escaped_emoji_in_script_data_is_valid_text(tmp_path: Path) -> None:
    script = 'var NOTES = { intro: "\\ud83d\\ude80 Launch day is here for everyone today" };'
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><script>{script}</script></body></html>", encoding="utf-8"
    )
    chunks = list(extract(p))
    for c in chunks:  # the index encodes every field as UTF-8
        (c.body + c.body_md + c.heading_path).encode("utf-8")
    assert "\U0001f680 Launch day" in "\n".join(c.body for c in chunks)


def test_a_shared_list_in_every_record_cannot_blow_the_budget(tmp_path: Path) -> None:
    big = ", ".join(f'"item number {i}"' for i in range(1000))
    rows = ", ".join("{ a: 'row', b: 'value', c: BIG }" for _ in range(1600))
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><script>var BIG = [{big}]; var DATA = [{rows}];</script></body></html>",
        encoding="utf-8",
    )
    started = time.monotonic()
    chunks = list(extract(p))
    assert time.monotonic() - started < 6
    assert sum(len(c.body_md) for c in chunks) < 6_000_000


def test_a_shared_table_shown_in_every_step_cannot_blow_the_budget(tmp_path: Path) -> None:
    rows = ", ".join(["['alpha', 'beta']"] * 2000)
    step = "{ say: 'See it.', views: [{ type: 'table', cols: ['a', 'b'], rows: B }] }"
    script = f"var B = [{rows}]; var F = {{}}; F.d = {{ steps: [{', '.join([step] * 1000)}] }};"
    p = tmp_path / "page.html"
    p.write_text(
        f'<html><body><h1>Doc</h1><div data-flow="d"></div><script>{script}</script></body></html>',
        encoding="utf-8",
    )
    started = time.monotonic()
    chunks = list(extract(p))
    assert time.monotonic() - started < 6
    assert sum(len(c.body_md) for c in chunks) < 6_000_000


def test_one_big_flat_list_in_a_record_is_linear(tmp_path: Path) -> None:
    words = ", ".join(f"'w{i}'" for i in range(40000))
    script = f"var BIG = [{words}]; var DATA = [{{ a: 'x', b: BIG }}, {{ a: 'y', b: 'z' }}];"
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><script>{script}</script></body></html>", encoding="utf-8"
    )
    started = time.monotonic()
    list(extract(p))
    assert time.monotonic() - started < 3


def test_prose_with_semicolons_and_entities_is_kept(tmp_path: Path) -> None:
    script = (
        "var DATA = { notes: ['Prices rose; sales fell over the whole summer period', "
        "'Fish &amp; chips are sold on the pier every day'] };"
    )
    p = tmp_path / "page.html"
    p.write_text(
        f"<html><body><h1>Doc</h1><script>{script}</script></body></html>", encoding="utf-8"
    )
    text = "\n".join(c.body for c in extract(p))
    assert "Prices rose; sales fell" in text
    assert "Fish & chips" in text


def test_words_split_inside_a_wrapper_span_stay_whole(tmp_path: Path) -> None:
    html = (
        "<p>Read <span><span>T</span><span>HE</span></span> manual.</p>"
        "<table><tr><td><span><span>4</span><span>2</span></span></td></tr></table>"
    )
    body = _body(tmp_path, html)
    assert "Read THE manual." in body
    assert "42" in body


def test_deeply_nested_inline_markup_is_linear(tmp_path: Path) -> None:
    html = "<p>" + "<span>a " * 250 + "Z " * 100000 + "</span>tail. " * 250 + "</p>"
    started = time.monotonic()
    _body(tmp_path, html)
    assert time.monotonic() - started < 3
