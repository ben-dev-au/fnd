"""Every documented query construct, alone and combined, finds what it should and
shows its match, through the path the TUI and the CLI share.

Each construct has hand-written expected files (tests/_syntax_corpus.py). A
combination's expectation is the set algebra of its parts' chunks, since a
boolean holds per chunk. Every result must also pass the TUI's own check that
its match can be shown (no "match not shown here")."""

from __future__ import annotations

import itertools
import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from fnd.index import build_index
from fnd.layered import search_layered
from fnd.matching import MatchSpec
from fnd.query import Searcher
from fnd.query_plan import QueryPlan
from fnd.tui.match_evidence import evidence_spec_for_pass, has_paintable_match
from fnd.tui.results_view import materialise_upfront
from tests import _syntax_corpus as corpus

Chunks = frozenset[tuple[str, int]]
# The lexical pass, and fusion's phrase pass (fnd.fusion._SOURCE_TO_PASS_INDEX).
_EXACT_PASSES: Final = frozenset({0, 3})

# (name, query, expected files). These combine with each other below.
PIECES: list[tuple[str, str, str]] = [
    ("term", "zephyr", "ZQTO"),
    ("term_b", "quokka", "QTN"),
    ("term_c", "marmalade", "QRO"),
    ("term_d", "tundra", "ZPN"),
    ("term_e", "obsidian", "PRTD"),
    ("term_f", "falcon", "PTD"),
    ("term_g", "lantern", "QPTD"),
    ("term_h", "harbour", "RT"),
    ("term_i", "attack", "ZQ"),
    ("phrase_stopwords", '"man in the middle"', "Z"),
    ("phrase_hyphenated", '"cross entropy loss"', "Z"),
    ("phrase_inner_stopword", '"crossed the tundra"', "Z"),
    ("proximity", "{5} zephyr tundra", "Z"),
    ("near", "zephyr NEAR/5 tundra", "Z"),
    ("typed_slop", '"zephyr tundra"~5', "Z"),
    ("proximity_far_apart", "{5} cross entropy", "Z"),
    ("proximity_wildcard", "{3} crypto* keys", "Z"),
    ("proximity_fuzzy", "{5} cryptography~ keys", "Z"),
    ("proximity_wide", "{60} quokka marmalade", "Q"),
    ("fuzzy_1", "mitochondira~1", "O"),
    ("fuzzy_2", "mitochondira~2", "ON"),
    ("fuzzy_far", "kubernates~2", "R"),
    ("fuzzy_first_letter", "kryptography~1", "Z"),
    ("fuzzy_bare", "cryptograpy~", "ZN"),
    ("prefix", "crypto*", "ZN"),
    ("suffix", "*ization", "ZPR"),
    ("infix", "crypt*aphy", "Z"),
    ("one_char", "gr?y", "QP"),
    ("regex", "/cryptograph(y|ic)/", "ZN"),
]
# (name, query, expected files): constructs and README examples that stand alone.
SOLO: list[tuple[str, str, str]] = [
    ("typo_respelt", "crytography", "ZN"),
    ("quoted_single_word", '"zephyr"', "ZQTO"),
    ("lowercase_or", "zephyr or harbour", "ZQTOR"),
    ("lowercase_and", "zephyr and harbour", "ZQTOR"),
    ("stopword_dropped", "the zephyr", "ZQTO"),
    ("signed_group", "falcon -(obsidian OR lantern)", "DT"),
    ("required_group", "+(zephyr OR quokka) marmalade", "ZQTON"),
    ("proximity_then_filter", "{10} zephyr tundra kind:md", "Z"),
    ("scope_and_frontmatter", "c:alpha zephyr [Course == 'Distributed Systems']", "Z"),
    ("quoted_field_values", 'title:"zephyr notes" heading_path:proof', "Z"),
    ("slides_after", "kind:pptx slide:>2 attention", "D"),
    ("recent_wildcard", "mtime:month crypto*", "ZN"),
    ("wildcard_and", "crypto* AND keys", "Z"),
    ("group_with_fuzzy", "(marmalade OR harbour) AND obsidian~1", "RT"),
    ("phrase_or_term", '"man in the middle" OR falcon', "ZPTD"),
    ("filter_alone", "kind:pdf", "T"),
    ("scope_alone", "c:beta", "DON"),
    ("frontmatter_alone", "[Course == 'Machine Learning']", "Q"),
]
# (name, clause, files it admits): filters that hold per file.
FILE_FILTERS: list[tuple[str, str, str]] = [
    ("kind_pdf", "kind:pdf", "T"),
    ("kind_md", "kind:md", "ZQPON"),
    ("kind_txt", "kind:txt", "R"),
    ("kind_pptx", "kind:pptx", "D"),
    ("title", "title:zephyr", "Z"),
    ("author_note", "author:dijkstra", "Z"),
    ("author_pdf", "author:knuth", "T"),
    ("path", "path_tokens:thesis", "T"),
    ("title_group", "title:(zephyr OR quokka)", "ZQ"),
    ("has_author", "has:author", "ZT"),
    ("one_collection", "c:alpha", "ZQPRT"),
    ("other_collection", "c:beta", "DON"),
    ("two_collections", "c:alpha,beta", "ZQPRTDON"),
    ("today", "mtime:today", "N"),
    ("week", "mtime:week", "NZ"),
    ("month", "mtime:month", "NZQ"),
    ("year", "mtime:year", "NZQP"),
    ("after_date", "mtime:>2025-01-01", "NZQPR"),
    ("date_range", "mtime:[2024-01-01 TO 2024-12-31]", "TD"),
    ("fm_equals", "[Course == 'Distributed Systems']", "Z"),
    ("fm_or", "[Notes_Type == 'Lecture' OR Notes_Type == 'Tutorial']", "ZQ"),
    ("fm_list_holds", "[Notes_Type == 'Cheat Sheet']", "Q"),
    ("fm_and", "[Course == 'Machine Learning' AND Year >= 2023]", "Q"),
    ("fm_in", "['urgent' in tags]", "Z"),
    # A note without the field passes a negated test; a bare string is no list.
    ("fm_not_group", "[NOT ('private' in tags)]", "ZPNOR"),
    ("fm_not_in", "['private' not in tags]", "Z"),
    ("fm_glob", "[Course ~~ 'Data *']", "O"),
    ("fm_spaced_name", '["Due Date" < 2026-01-01]', "Z"),
    ("fm_missing_field", "[Status != 'draft']", ""),
]


@dataclass(frozen=True)
class _Hit:
    letter: str
    seq: int
    page: int
    slide: int
    heading: str
    exact: bool


# (name, clause, which of a construct's chunks it keeps): filters that hold per chunk.
CHUNK_FILTERS: list[tuple[str, str, Callable[[_Hit], bool]]] = [
    ("page", "page:2", lambda h: h.page == 2),
    ("page_after", "page:>20", lambda h: h.page > 20),
    ("page_range", "page:[1 TO 2]", lambda h: 1 <= h.page <= 2),
    ("page_before", "page:<3", lambda h: 1 <= h.page < 3),
    ("slide_before", "slide:<5", lambda h: 1 <= h.slide < 5),
    ("heading", 'heading_path:"chapter 4"', lambda h: "chapter 4" in h.heading.lower()),
]
OPS: dict[str, tuple[str, Callable[[Chunks, Chunks], Chunks]]] = {
    "and": ("{a} AND {b}", lambda a, b: a & b),
    "or": ("{a} OR {b}", lambda a, b: a | b),
    "not": ("{a} NOT {b}", lambda a, b: a - b),
    "adjacent": ("{a} {b}", lambda a, b: a | b),
    "plus_minus": ("+{a} -{b}", lambda a, b: a - b),
}
NESTED: list[tuple[str, Callable[[Chunks, Chunks, Chunks], Chunks]]] = [
    ("({a} OR {b}) AND {c}", lambda a, b, c: (a | b) & c),
    ("{a} AND ({b} OR {c})", lambda a, b, c: a & (b | c)),
    ("({a} AND {b}) OR {c}", lambda a, b, c: (a & b) | c),
    ("{a} NOT ({b} OR {c})", lambda a, b, c: a - (b | c)),
    ("{a} -({b} OR {c})", lambda a, b, c: a - (b | c)),
]


@dataclass
class _Result:
    files: list[str] = field(default_factory=list)
    hits: list[_Hit] = field(default_factory=list)
    unshown: list[str] = field(default_factory=list)

    @property
    def found(self) -> str:
        return "".join(sorted(set(self.files)))

    def chunks(self, keep: Callable[[_Hit], bool] = lambda _h: True) -> Chunks:
        """The chunks the search itself matched, as the combinations' oracle."""
        return frozenset((h.letter, h.seq) for h in self.hits if h.exact and keep(h))


def _letter(path: str) -> str:
    return corpus.LETTER.get(Path(path).name, Path(path).name)


def run(searcher: Searcher, query: str, *, every_chunk: bool = False) -> _Result:
    """``query`` as the TUI's worker runs it, then the TUI's evidence check per row."""
    plan = QueryPlan.from_user_text(query)
    if not (plan.lexical.strip() or plan.metadata_filter):
        return _Result()
    groups, trace = search_layered(
        searcher,
        query=plan.lexical,
        limit=50,
        sections_per_file=1000 if every_chunk else 200,
        sections_score_threshold=0.0 if every_chunk else 0.5,
        metadata_filter=plan.metadata_filter,
        with_trace=True,
    )
    strict = MatchSpec.from_query(plan.lexical, auto_fuzzy=False)
    paint = trace.paint_spec(MatchSpec.from_query(plan.lexical), strict)
    evidence = trace.resolve(strict)
    groups = materialise_upfront(groups, paint, intent=None)
    out = _Result(files=[_letter(g.path) for g in groups])
    for g in groups:
        for h in g.hits:
            out.hits.append(
                _Hit(
                    _letter(h.path),
                    h.chunk_seq,
                    h.page,
                    h.slide,
                    h.heading_path or "",
                    h.pass_index in _EXACT_PASSES,
                )
            )
            spec = evidence_spec_for_pass(h.pass_index, strict=evidence, painting=paint)
            if not has_paintable_match(h, spec):
                out.unshown.append(f"{_letter(h.path)}#{h.chunk_seq}")
    return out


def _grouped(query: str) -> str:
    """A multi-word piece bracketed, so ``{N}`` and NEAR bind only their own words."""
    return f"({query})" if " " in query else query


@pytest.fixture
def searcher(tmp_path: Path, tmp_index_dir: Path) -> Searcher:
    corpus.build(tmp_path / "corpus")
    build_index(roots=[tmp_path / "corpus" / "alpha"], index_dir=tmp_index_dir, collection="alpha")
    build_index(roots=[tmp_path / "corpus" / "beta"], index_dir=tmp_index_dir, collection="beta")
    return Searcher(index_dir=tmp_index_dir)


def _check(searcher: Searcher, cases: Sequence[tuple[str, str | Chunks]]) -> None:
    """Every ``(query, expected)`` is found exactly and shows its match. Expected
    chunks also pin the exact sections, so a per-section bug cannot hide in its file."""
    wrong = []
    for query, expected in cases:
        sections = not isinstance(expected, str)
        got = run(searcher, query, every_chunk=sections)
        files = _files(expected) if sections else "".join(sorted(set(expected)))
        extra = missing = frozenset()
        if sections:
            extra, missing = got.chunks() - expected, expected - got.chunks()
        if got.found != files or got.unshown or extra or missing:
            wrong.append(
                f"{query!r}: found {got.found or '-'}, expected {files or '-'}"
                + (
                    f", sections extra {sorted(extra)} missing {sorted(missing)}"
                    if extra or missing
                    else ""
                )
                + (f", match not shown in {got.unshown}" if got.unshown else "")
            )
    assert not wrong, f"{len(wrong)} of {len(cases)} wrong:\n" + "\n".join(wrong[:25])


def _pieces(searcher: Searcher) -> dict[str, tuple[str, _Result]]:
    return {name: (q, run(searcher, q, every_chunk=True)) for name, q, _ in PIECES}


def _files(chunks: Chunks) -> str:
    return "".join(sorted({letter for letter, _ in chunks}))


def test_every_construct_alone(searcher: Searcher) -> None:
    _check(searcher, [(q, exp) for _, q, exp in [*PIECES, *SOLO]])


@pytest.mark.parametrize("op", sorted(OPS))
def test_every_pair_of_constructs(searcher: Searcher, op: str) -> None:
    template, combine = OPS[op]
    pieces = _pieces(searcher)
    _check(
        searcher,
        [
            (
                template.format(a=_grouped(qa), b=_grouped(qb)),
                combine(ra.chunks(), rb.chunks()),
            )
            for (qa, ra), (qb, rb) in itertools.permutations(pieces.values(), 2)
        ],
    )


def test_nested_groups(searcher: Searcher) -> None:
    pieces = list(_pieces(searcher).values())
    rng = random.Random(7)
    cases = []
    for _ in range(300):
        (qa, ra), (qb, rb), (qc, rc) = rng.sample(pieces, 3)
        template, combine = rng.choice(NESTED)
        query = template.format(a=_grouped(qa), b=_grouped(qb), c=_grouped(qc))
        cases.append((query, combine(ra.chunks(), rb.chunks(), rc.chunks())))
    _check(searcher, cases)


def test_every_filter_constrains_every_construct(searcher: Searcher) -> None:
    pieces = _pieces(searcher)
    cases = [
        (f"{clause} {_grouped(q)}", frozenset(c for c in r.chunks() if c[0] in admits))
        for _, clause, admits in FILE_FILTERS
        for q, r in pieces.values()
    ]
    cases += [
        (f"{clause} {_grouped(q)}", r.chunks(keep))
        for _, clause, keep in CHUNK_FILTERS
        for q, r in pieces.values()
    ]
    _check(searcher, cases)


# Every example the README's search how-to shows, by the case above that exercises it.
README_EXAMPLES: dict[str, str] = {
    "entropy": "term",
    "cross entropy loss": "and",
    "cross AND entropy": "and",
    '"cross entropy loss"': "phrase_hyphenated",
    "cross OR entropy": "or",
    "entropy NOT regression": "not",
    "+rust -python": "plus_minus",
    "rust -(python OR java)": "signed_group",
    "(loss OR cost) AND function": "nested",
    "man in the middle": "adjacent",
    '"man in the middle"': "phrase_stopwords",
    "{5} cross entropy": "proximity",
    "cross NEAR/5 entropy": "near",
    "{20} man in the middle attack": "proximity_wide",
    "{60} buffer overflow exploit": "proximity_wide",
    "{500} race condition mitigations": "proximity_wide",
    "mitochondira~1": "fuzzy_1",
    "kubernates~2": "fuzzy_far",
    "title:transformer": "title",
    'heading_path:"chapter 4"': "heading",
    "author:dijkstra": "author_note",
    "kind:pdf": "kind_pdf",
    "path_tokens:thesis": "path",
    "title:(rust OR golang)": "title_group",
    "has:author": "has_author",
    "c:security attack": "one_collection",
    "c:notes,papers transformer": "two_collections",
    "page:5": "page",
    "page:>20": "page_after",
    "page:[10 TO 20]": "page_range",
    "slide:<5": "slide_before",
    "mtime:today": "today",
    "mtime:week` / `mtime:month` / `mtime:year": "week",
    "mtime:>2024-01-01": "after_date",
    "mtime:[2024-01-01 TO 2024-06-30]": "date_range",
    "crypto*": "prefix",
    "*ization": "suffix",
    "crypt*aphy": "infix",
    "gr?y": "one_char",
    "/cryptograph(y|ic)/": "regex",
    "mitm [Course == 'Distributed Systems']": "fm_equals",
    "[Notes_Type == 'Lecture' OR Notes_Type == 'Tutorial']": "fm_or",
    "[Notes_Type == 'Cheat Sheet']": "fm_list_holds",
    "entropy [Course == 'ML' AND Year >= 2024]": "fm_and",
    "['urgent' in tags]": "fm_in",
    "[NOT ('private' in tags)]": "fm_not_group",
    "[Course ~~ 'Data *']": "fm_glob",
    '["Due Date" < 2026-01-01]': "fm_spaced_name",
    '"buffer overflow"': "phrase_hyphenated",
    "{10} buffer overflow exploit kind:pdf": "proximity_then_filter",
    "c:notes mitm [Course == 'Distributed Systems']": "scope_and_frontmatter",
    'title:"chapter 4" heading_path:proof': "quoted_field_values",
    "kind:pptx slide:>10 attention": "slides_after",
    "mtime:month crypto*": "recent_wildcard",
    "crypto* AND wallet": "wildcard_and",
    "(loss OR cost) AND function~1": "group_with_fuzzy",
    '"defence in depth" OR diverse': "phrase_or_term",
}


def _readme_examples() -> list[str]:
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("## Search how-to") : readme.index("## Contributing")]
    found = [
        m.group(1).replace("\\|", "|") for m in re.finditer(r"^\| `(.+?)`\s+\|", section, re.M)
    ]
    block = re.search(r"### Composing: worked examples\n\n```text\n(.*?)```", section, re.S)
    assert block, "the README lost its worked examples"
    found += [q for line in block.group(1).splitlines() if (q := line.split("  #")[0].strip())]
    return found


def test_every_readme_example_is_in_the_matrix() -> None:
    """A documented example with no case here is syntax nothing checks."""
    known = {n for n, _, _ in [*PIECES, *SOLO, *FILE_FILTERS]}
    known |= {n for n, _, _ in CHUNK_FILTERS} | set(OPS) | {"nested"}
    examples = _readme_examples()
    unmapped = [q for q in examples if q not in README_EXAMPLES]
    assert not unmapped, f"README examples with no matrix case: {unmapped}"
    assert not {c for c in README_EXAMPLES.values() if c not in known}
    assert not set(README_EXAMPLES) - set(examples), "a mapped example left the README"


def test_every_readme_example_runs(searcher: Searcher) -> None:
    for example in _readme_examples():
        for query in example.split("` / `"):
            run(searcher, query)


# The matrix's hardest cases, typed into the app and into ``fnd search``.
SURFACE_QUERIES = [q for _, q, _ in SOLO] + [
    "zephyr AND tundra",
    "{3} crypto* keys",
    "{5} cryptography~ keys",
    "zephyr NOT mitochondira~1",
    "zephyr AND /cryptograph(y|ic)/",
    "tundra -(harbour OR mitochondira~2)",
    "slide:<5 lantern",
    "page:<3 zephyr",
    "author:dijkstra tundra",
    "[NOT ('private' in tags)] marmalade",
    "c:alpha,beta obsidian",
]


@pytest.mark.asyncio
async def test_the_tui_finds_what_search_finds(searcher: Searcher, tmp_index_dir: Path) -> None:
    from fnd.tui import FNDApp
    from tests._pilot_wait import run_search

    app = FNDApp(index_dir=tmp_index_dir)
    async with app.run_test() as pilot:
        await pilot.pause()
        wrong = []
        for query in SURFACE_QUERIES:
            await run_search(pilot, app, query)
            shown = "".join(sorted({_letter(g.path) for g in app._search.groups}))
            if shown != run(searcher, query).found:
                wrong.append(f"{query!r}: the TUI shows {shown or '-'}")
        assert not wrong, "\n".join(wrong)


def test_the_cli_finds_what_search_finds(
    searcher: Searcher,
    tmp_path: Path,
    tmp_index_dir: Path,
    isolated_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from fnd import cli

    isolated_config_path.write_text(
        "".join(
            f'[[collections.{name}.sources]]\npath = "{(tmp_path / "corpus" / name).as_posix()}"\n'
            for name in ("alpha", "beta")
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "default_index_dir", lambda: tmp_index_dir)
    wrong = []
    for query in SURFACE_QUERIES:
        result = CliRunner().invoke(cli.app, ["search", query, "--limit", "50"])
        names = re.findall(r"[\w.]+\.(?:md|txt|pdf|pptx)\b", result.output)
        shown = "".join(sorted({corpus.LETTER[n] for n in names if n in corpus.LETTER}))
        if result.exit_code or shown != run(searcher, query).found:
            wrong.append(f"{query!r}: the CLI shows {shown or '-'} (exit {result.exit_code})")
    assert not wrong, "\n".join(wrong)


def test_a_rule_alone_is_traced_as_typed(searcher: Searcher) -> None:
    """Explain names the rule the user typed, not the note-kind query that runs it."""
    _, trace = search_layered(
        searcher,
        query="",
        metadata_filter="Course == 'Machine Learning'",
        limit=10,
        with_trace=True,
    )
    assert trace.query == "[Course == 'Machine Learning']"
