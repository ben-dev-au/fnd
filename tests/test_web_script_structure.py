"""Structure recovered from inline script data: steps, fenced output, tables."""

from __future__ import annotations

from pathlib import Path

from fnd.extract import extract
from fnd.extract.base import Chunk

_PAGE = """<html><body><h1>Guide</h1>
<section><h2><span class="n">2</span>Retries</h2><div data-flow="retry"></div></section>
<script>{script}</script></body></html>"""

_SCRIPT = """
var KIND = { T: "Transformation: planned only", A: "Action: runs now" };
var COLS = ["host", "status"];
var R1 = ["alpha", "200"];
var R2 = ["beta", "503"];
var FLOWS = {};
FLOWS.retry = {
  code: ["client = Client(url)", "client.fetch()"],
  steps: [
    { line: 0, kind: "T", say: "Build the client. Nothing is sent yet, so no error appears.",
      views: [{ type: "rows", items: [1, 2].map(function (n) { return C(String(n)); }) }] },
    { line: 1, kind: "A", say: "Fetching sends the request and the first reply comes back.",
      views: [{ type: "table", cols: COLS, rows: [R1, R2], note: "Beta is failing." },
              { type: "out", cmd: "client.fetch()", out: "200 OK\\nretries: 0" }] }
  ]
};
"""


def _chunks(tmp_path: Path, script: str = _SCRIPT) -> list[Chunk]:
    p = tmp_path / "guide.html"
    p.write_text(_PAGE.format(script=script), encoding="utf-8")
    return list(extract(p))


def _md(chunks: list[Chunk]) -> str:
    return "\n\n".join(c.body_md for c in chunks)


def test_steps_are_numbered_and_labelled_with_the_pages_own_kinds(tmp_path: Path) -> None:
    md = _md(_chunks(tmp_path))
    assert "**Step 1 · Transformation**" in md
    assert "**Step 2 · Action**" in md
    assert md.index("Step 1") < md.index("Step 2")


def test_a_step_shows_its_code_line_then_its_output_in_the_next_fence(tmp_path: Path) -> None:
    md = _md(_chunks(tmp_path))
    assert "```\nclient.fetch()\n```\n\n```\n200 OK\nretries: 0\n```" in md


def test_the_program_listing_is_a_fenced_block(tmp_path: Path) -> None:
    assert "```\nclient = Client(url)\nclient.fetch()\n```" in _md(_chunks(tmp_path))


def test_table_views_resolve_named_constants(tmp_path: Path) -> None:
    md = _md(_chunks(tmp_path))
    assert "| host | status |" in md
    assert "| beta | 503 |" in md
    assert "Beta is failing." in md


def test_unparseable_expressions_do_not_hide_later_steps(tmp_path: Path) -> None:
    assert "Fetching sends the request" in _md(_chunks(tmp_path))


def test_a_list_of_records_is_not_numbered_as_steps(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.retry = { legend: [
      { id: 1, description: "Verified in a primary source such as a regulator filing." },
      { id: 2, description: "Secondary source only, so check before quoting it." }] };"""
    md = _md(_chunks(tmp_path, script))
    assert "Verified in a primary source" in md
    assert "Secondary source only" in md
    assert "Step 1" not in md


_HUB = """var DATA = { keyStats: [
  { label: "Visitors", value: "12,480", src: { label: "Survey summary", url: "https://example.com/r" } },
  { label: "Sales", value: "3,100", src: { label: "Survey summary", url: null } }],
  notes: ["Figures are underlying unless stated otherwise.", "Kiosk sales are excluded from the harbour view."] };"""


def test_a_list_of_records_becomes_a_table_with_linked_sources(tmp_path: Path) -> None:
    md = _md(_chunks(tmp_path, _HUB))
    assert "| Label | Value | Src |" in md
    assert "| Visitors | 12,480 | [Survey summary](https://example.com/r) |" in md
    assert "| Sales | 3,100 | Survey summary |" in md


def test_markup_inside_record_cells_is_converted_not_shown_raw(tmp_path: Path) -> None:
    script = """var STEPS = [
      { n: 1, work: "d1: 1 / 8 = <b>0.125</b><br>d2: 0 / 13 = <b>0</b>", why: "TF divides the count." },
      { n: 2, work: "Use <code>features</code> as the column.", why: "One vector per row." }];"""
    md = _md(_chunks(tmp_path, script))
    for raw in ("<b>", "<br>", "<code>"):
        assert raw not in md
    assert "d1: 1 / 8 = **0.125** / d2: 0 / 13 = **0**" in md
    assert "Use `features` as the column." in md
    body = "\n".join(c.body for c in _chunks(tmp_path, script))
    assert "0.125" in body
    assert "**" not in body


def test_entities_in_record_cells_are_decoded(tmp_path: Path) -> None:
    script = """var CALLS = [
      { n: 1, url: "?page=0&amp;sort=name", why: "The first request loads page zero." },
      { n: 2, url: "?page=1&amp;sort=name", why: "The second request loads page one." }];"""
    md = _md(_chunks(tmp_path, script))
    assert "?page=0&sort=name" in md
    assert "&amp;" not in md


def test_records_with_sparse_or_nested_fields_lose_nothing(tmp_path: Path) -> None:
    script = """var DATA = { facts: [
      { label: "Piers", value: "70 berths across the harbour today.", note: "Counted in August." },
      { label: "Moorings", value: "9,000 licensed moorings in total.",
        detail: { caveat: "Excludes day-use buoys.", year: "2026" } }] };"""
    body = "\n".join(c.body for c in _chunks(tmp_path, script))
    for fragment in ("Counted in August.", "Excludes day-use buoys.", "70 berths across"):
        assert fragment in body


_CITED = """var DATA = { claims: [
  { text: "Visitors grew eight per cent over the summer.",
    srcs: [{ label: "Field notes, p.14", url: "https://example.com/notes.pdf" },
           { label: "Harbour log, p.9", url: "https://example.com/log.pdf" }] },
  { text: "Ferry numbers fell after the timetable change.", srcs: [] },
  { text: "Queues stayed short across both piers.",
    srcs: [{ label: "Survey deck, slide 6", url: null }] }] };"""


def test_a_records_citations_are_links_in_its_row(tmp_path: Path) -> None:
    chunks = _chunks(tmp_path, _CITED)
    md = _md(chunks)
    assert "[Field notes, p.14](https://example.com/notes.pdf); [Harbour log, p.9]" in md
    assert "| Queues stayed short across both piers. | Survey deck, slide 6 |" in md
    assert "| Label | Url |" not in md
    assert not any(c.heading_path.endswith("Srcs") for c in chunks)


def test_a_standalone_citation_list_is_bullet_links_without_its_own_section(
    tmp_path: Path,
) -> None:
    script = """var DATA = { intro: "Everything here comes from the published survey pack.",
      links: [{ label: "Field notes", url: "https://example.com/notes.pdf" },
              { label: "Harbour log", url: "https://example.com/log.pdf" }] };"""
    chunks = _chunks(tmp_path, script)
    md = _md(chunks)
    assert "- [Field notes](https://example.com/notes.pdf)" in md
    assert "- [Harbour log](https://example.com/log.pdf)" in md
    assert not any(c.heading_path.endswith("Links") for c in chunks)


def test_only_the_label_of_a_citation_is_indexed(tmp_path: Path) -> None:
    body = "\n".join(c.body for c in _chunks(tmp_path, _CITED))
    assert "Field notes, p.14" in body
    assert "https://example.com" not in body


def test_unattached_data_sections_become_headed_chunks(tmp_path: Path) -> None:
    chunks = _chunks(tmp_path, _HUB)
    paths = [c.heading_path for c in chunks]
    assert "Guide > Script text > Key stats" in paths
    assert "Guide > Script text > Notes" in paths
    stats = next(c for c in chunks if c.heading_path.endswith("Key stats"))
    assert "| Label | Value | Src |" in stats.body_md


def test_data_attached_to_a_page_node_leads_in_bold_without_new_sections(tmp_path: Path) -> None:
    script = "FLOWS = {}; FLOWS.retry = " + _HUB.split("DATA = ", 1)[1]
    chunks = _chunks(tmp_path, script)
    assert [c.heading_path for c in chunks] == ["Guide", "Guide > 2 Retries"]
    md = chunks[-1].body_md
    assert md.index("**Key stats**") < md.index("| Label |")


def test_a_list_of_sentences_becomes_bullets(tmp_path: Path) -> None:
    md = _md(_chunks(tmp_path, _HUB))
    assert "- Figures are underlying unless stated otherwise." in md
    assert "- Kiosk sales are excluded from the harbour view." in md


def test_a_short_code_field_is_an_identifier_not_a_program(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.retry = { goals: [
      { code: "DD", text: "Determine data value in every decision the team makes." }] };"""
    md = _md(_chunks(tmp_path, script))
    assert "```" not in md
    assert "`DD`" in md


def test_a_terminal_view_becomes_one_fenced_transcript(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.retry = { steps: [
      { say: "The prompt shows where you are.",
        views: [{ type: "term", lines: [{ p: "$", c: "ls /data" }, { o: "a.csv\\nb.csv" }] }] }] };"""
    assert "$ ls /data\na.csv\nb.csv" in _md(_chunks(tmp_path, script))


def test_a_terminal_step_does_not_repeat_the_command_its_transcript_shows(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.retry = { code: ["ls /data"], steps: [
      { line: 0, say: "List the folder before you read anything.",
        views: [{ type: "term", lines: [{ p: "$", c: "ls /data" }, { o: "a.csv" }] }] }] };"""
    assert _md(_chunks(tmp_path, script)).count("ls /data") == 2  # listing + transcript


def test_view_captions_join_into_one_italic_line(tmp_path: Path) -> None:
    script = """FLOWS = {}; FLOWS.retry = { steps: [
      { say: "Cut the list into three partitions.",
        views: [{ type: "parts", label: "rdd", sub: "RDD in 3 partitions", parts: [] }] }] };"""
    assert "*rdd · RDD in 3 partitions*" in _md(_chunks(tmp_path, script))


def test_script_data_only_attaches_to_id_and_data_attributes(tmp_path: Path) -> None:
    p = tmp_path / "role.html"
    p.write_text(
        '<html><body><h1>Top</h1><div role="group"><button>A+</button></div>'
        '<h2>Later</h2><div data-flow="group"></div>'
        '<script>FLOWS = {}; FLOWS.group = { say: "Grouping keeps every value for a key." };'
        "</script></body></html>",
        encoding="utf-8",
    )
    by_heading = {c.heading_path: c.body for c in extract(p)}
    assert "Grouping keeps" in by_heading["Top > Later"]
    assert "Grouping keeps" not in by_heading["Top"]


def test_broken_script_text_never_raises(tmp_path: Path) -> None:
    script = "FLOWS.retry = { steps: [ { say: 'unterminated ], x: ( [ { }"
    assert _chunks(tmp_path, script)


def test_heading_number_badge_is_separated_from_the_title(tmp_path: Path) -> None:
    assert [c.heading_path for c in _chunks(tmp_path)][-1] == "Guide > 2 Retries"


_TASKS = """<html><body><h1>Guide</h1><ol>
<li><p class="q"><b>Q1</b>Add one to each element.<span class="uses">Uses: map</span></p>
<details><summary>Show answer</summary><div>
<pre><code>rdd2 = rdd.map(lambda x: x + 1)
rdd2.collect()</code></pre>
<p class="outl">Output</p><pre>[2, 3]</pre>
<p class="why">map keeps the record count.</p></div></details></li>
<li><p class="q"><b>Q2</b>Keep the evens.</p></li>
</ol></body></html>"""


def _tasks_md(tmp_path: Path) -> str:
    p = tmp_path / "tasks.html"
    p.write_text(_TASKS, encoding="utf-8")
    return "\n\n".join(c.body_md for c in extract(p))


def test_a_list_item_keeps_its_answer_blocks_as_blocks(tmp_path: Path) -> None:
    md = _tasks_md(tmp_path)
    assert "```\nrdd2 = rdd.map(lambda x: x + 1)\nrdd2.collect()\n```" in md
    assert "```\n[2, 3]\n```" in md
    assert "map keeps the record count." in md
    assert "**Show answer**" in md  # a summary can be the question itself, so it is kept


def test_ordered_items_keep_their_numbers_across_block_content(tmp_path: Path) -> None:
    md = _tasks_md(tmp_path)
    assert "1. **Q1**" in md
    assert "2. **Q2**" in md


def test_list_items_after_a_block_item_still_render(tmp_path: Path) -> None:
    assert "Keep the evens." in _tasks_md(tmp_path)


def test_labels_and_trailing_spans_are_spaced_from_their_neighbours(tmp_path: Path) -> None:
    md = _tasks_md(tmp_path)
    assert "**Q1** Add one to each element. Uses: map" in md


def test_a_stage_banner_opens_its_own_section_instead_of_trailing_the_previous(
    tmp_path: Path,
) -> None:
    p = tmp_path / "stages.html"
    p.write_text(
        "<html><body><h1>Guide</h1><section><h2>One</h2><p>First body.</p></section>"
        '<div class="stage-h"><b>Pairs</b><span>Counting per key</span></div>'
        "<section><h2>Two</h2><p>Second body.</p></section>"
        '<div class="note"><b>Note</b><span>Plain callout.</span></div></body></html>',
        encoding="utf-8",
    )
    by_heading = {c.heading_path: c.body for c in extract(p)}
    assert "Counting per key" in by_heading["Guide > Pairs"]
    assert "Pairs" not in by_heading["Guide > One"]
    assert "Plain callout." in by_heading["Guide > Two"]


def test_adjacent_spans_do_not_fuse_words(tmp_path: Path) -> None:
    p = tmp_path / "legend.html"
    p.write_text(
        "<html><body><h1>L</h1><ul><li><span><span>Action</span></span>"
        "<span>Runs the plan.</span></li></ul></body></html>",
        encoding="utf-8",
    )
    assert "Action Runs the plan." in "\n".join(c.body for c in extract(p))
