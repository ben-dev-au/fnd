"""Lower ``field:value`` / ``c:name`` / range clauses out of a query string into
typed tantivy filter queries (hard, unscored), leaving the scored content behind.

This is the "filter context" half of the engine (Elasticsearch / Quickwit
pattern): structural and field qualifiers restrict the result set without
affecting BM25 score, while bare terms and phrases stay in the scored content
query.

Extraction is deliberately conservative: a ``field:`` clause is lifted only when
it sits at the top level (not inside ``(...)``) and is not adjacent to a boolean
operator. Boolean-composed field clauses (``kind:pdf OR kind:docx``) are left in
the content query for tantivy's parser to handle — never silently mis-filtered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tantivy
from tantivy import FieldType, Query

from fnd.analysis import analyse
from fnd.kinds import KINDS_IN_CATEGORY
from fnd.query_fields import FieldSpec, FieldValue, date_token_range, resolve
from fnd.query_spans import literal_spans

_BOOL_OPS = frozenset({"AND", "OR", "NOT"})
# field:value head — value captured greedily (the tokenizer already kept any
# bracketed/quoted run together as one token).
_CLAUSE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.+)$", re.DOTALL)
# ``[`` / ``]`` include a bound, ``{`` / ``}`` exclude it, mixed freely.
_RANGE_RE = re.compile(r"^([\[{])\s*(.+?)\s+TO\s+(.+?)\s*([\]}])$", re.IGNORECASE)
_SLOPPY_PHRASE_RE = re.compile(r'^"(.*)"~(\d+)$', re.DOTALL)
_FIELD_NAME_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*):")
_CMP_RE = re.compile(r"^(>=|<=|>|<)(.+)$")


@dataclass(frozen=True)
class ExtractResult:
    content: str
    filters: list[Query]


def _tokenize_top_level(s: str) -> list[str]:
    """Split ``s`` into whitespace-separated top-level tokens, keeping any
    quoted phrase or ``/regex/`` (see :mod:`fnd.query_spans`) and any ``[…]`` /
    ``(…)`` run or ``field:{…}`` range, with its inner spaces, intact."""
    tokens: list[str] = []
    buf: list[str] = []
    # The closers each open run waits for: a range ends on ``]`` or ``}``
    # (``[lo TO hi}``), and ``{`` opens one only as a bound (``page:{1 TO 9}``),
    # so a proximity ``{N}`` neither opens nor closes anything.
    awaiting: list[str] = []
    literal_ends = {span.start: span.end for span in literal_spans(s)}
    i = 0
    while i < len(s):
        if i in literal_ends:
            buf.append(s[i : literal_ends[i]])
            i = literal_ends[i]
            continue
        ch = s[i]
        i += 1
        if ch == "(":
            awaiting.append(")")
        elif ch == "[" or (ch == "{" and buf and buf[-1] == ":"):
            awaiting.append("]}")
        elif awaiting and ch in awaiting[-1]:
            awaiting.pop()
        elif ch.isspace() and not awaiting:
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        return s[1:-1]
    return s


def scan_exact_values(query: str) -> list[tuple[str, str]]:
    """Every ``kind:``/``c:`` value a query names, as ``(field, value)`` rows.

    Reuses the tokenizer and clause grammar that lower these clauses into
    filters, so a caller validating the values can't disagree with the engine
    about what counts as one. Groups (``kind:(a b)``) and collection comma
    lists (``c:a,b``) are split exactly the way :func:`_compile` splits them —
    notably ``kind:a,b`` is NOT split, because the compiler treats it as a
    single term.

    Only EXACT fields are reported: they're the ones backed by a closed set of
    legal values, so an unrecognised one can never match. Recurses into
    ``(...)`` groups, which ``extract_filters`` leaves for tantivy's parser.
    """
    found: list[tuple[str, str]] = []
    _scan_exact_into(query, found)
    return found


def _scan_exact_into(s: str, found: list[tuple[str, str]]) -> None:
    for token in _tokenize_top_level(s):
        if token.startswith("(") and token.endswith(")"):
            _scan_exact_into(token[1:-1], found)
            continue
        m = _CLAUSE_RE.match(token)
        if not m:
            continue
        spec = resolve(m.group(1))
        if spec is None or spec.value is not FieldValue.EXACT:
            continue
        found.extend((spec.query_name, v) for v in _exact_values(spec, m.group(2)))


def _exact_values(spec: FieldSpec, value: str) -> list[str]:
    """Split one EXACT clause value into the terms it will compile to."""
    if value.startswith("(") and value.endswith(")"):
        return [
            _strip_quotes(t) for t in _tokenize_top_level(value[1:-1]) if t.upper() not in _BOOL_OPS
        ]
    if spec.query_name == "collection":
        return [_strip_quotes(p.strip()) for p in value.split(",") if p.strip()]
    stripped = _strip_quotes(value).strip()
    return [stripped] if stripped else []


def _uint_range(spec: FieldSpec, value: str, schema: tantivy.Schema) -> Query | None:
    """Compile a UINT field value (point / [lo TO hi] / >N / mtime token)."""
    assert spec.coerce is not None
    field = spec.tantivy_field

    def rng(lo: int | None, hi: int | None, inc_lo: bool = True, inc_hi: bool = True) -> Query:
        return Query.range_query(schema, field, FieldType.Unsigned, lo, hi, inc_lo, inc_hi, False)

    # An unparsable bound (``page:>abc``, ``mtime:[2024-13-01 TO 10]``) returns
    # None so the caller leaves the clause in content rather than crashing.
    try:
        m = _RANGE_RE.match(value)
        if m:
            lo, hi = spec.coerce(m.group(2)), spec.coerce(m.group(3))
            return rng(lo, hi, inc_lo=m.group(1) == "[", inc_hi=m.group(4) == "]")
        m = _CMP_RE.match(value)
        if m:
            op, n = m.group(1), spec.coerce(m.group(2))
            if op == ">":
                return rng(n, None, inc_lo=False)
            if op == ">=":
                return rng(n, None)
            low = spec.first or None
            if op == "<":
                return rng(low, n, inc_hi=False)
            return rng(low, n)  # <=
        if spec.query_name in ("mtime", "created"):
            tok = date_token_range(value)
            if tok is not None:
                return rng(tok[0], tok[1])
        return rng(spec.coerce(value), spec.coerce(value))  # bare point: page:5
    except ValueError:
        return None


def _compile(
    spec: FieldSpec, value: str, schema: tantivy.Schema, index: tantivy.Index | None
) -> Query | None:
    """Lower one ``field:value`` clause into a typed tantivy query, or None when
    the value can't be parsed (caller then leaves the clause in content)."""
    if spec.value is FieldValue.EXACT:
        if _group_has_logic(value):
            return None
        terms = [Query.term_query(schema, spec.tantivy_field, t) for t in _exact_terms(spec, value)]
        if not terms:
            return None
        if len(terms) == 1:
            return terms[0]
        return Query.boolean_query([(tantivy.Occur.Should, t) for t in terms])
    # Field grouping: ``title:(a OR b)`` → the boolean parsed against that field.
    # Needs the index (parse_query); without it, fall through to term/phrase.
    if index is not None and value.startswith("(") and value.endswith(")"):
        try:
            return index.parse_query(value, default_field_names=[spec.tantivy_field])
        except ValueError:
            return None
    if spec.value is FieldValue.UINT:
        return _uint_range(spec, value, schema)
    # TEXT fields use the index analyser: quoted → phrase (``"a b"~N`` sloppy),
    # single word → term.
    sloppy = _SLOPPY_PHRASE_RE.match(value)
    raw, slop = (sloppy.group(1), int(sloppy.group(2))) if sloppy else (_strip_quotes(value), 0)
    words = analyse(raw)
    if not words:
        return None
    if len(words) == 1:
        return Query.term_query(schema, spec.tantivy_field, words[0])
    return Query.phrase_query(schema, spec.tantivy_field, [*words], slop)


def _group_has_logic(value: str) -> bool:
    """An exact field's group is a list of alternatives; AND, NOT or a ``-``
    value in it would be read as one more alternative."""
    if not (value.startswith("(") and value.endswith(")")):
        return False
    tokens = _tokenize_top_level(value[1:-1])
    return any(t.upper() in {"AND", "NOT"} or t.startswith("-") for t in tokens)


def _exact_terms(spec: FieldSpec, value: str) -> list[str]:
    """The stored terms one EXACT clause matches: its values (a group, or a
    collection's comma list), each kind category as its member kinds, the way
    ``--kind`` expands it."""
    values = _exact_values(spec, value)
    if spec.query_name == "collection":
        return values
    terms = [v.lower() for v in values]
    if spec.query_name == "kind":
        return [kind for term in terms for kind in KINDS_IN_CATEGORY.get(term, (term,))]
    return terms


def compile_clause(
    name: str, value: str, schema: tantivy.Schema, index: tantivy.Index | None
) -> Query | None:
    """``name:value`` as the typed query a lifted filter compiles to, or None
    when ``name`` is no field or the value cannot be read."""
    spec = resolve(name)
    return _compile(spec, value, schema, index) if spec is not None else None


def extract_filters(
    query: str, schema: tantivy.Schema, index: tantivy.Index | None = None
) -> ExtractResult:
    """Split ``query`` into (scored content string, typed hard-filter queries).

    A ``field:value`` (or ``has:field`` presence) token is lifted only when it
    is at the top level and not adjacent to a boolean operator; everything else
    stays in ``content``. ``index`` enables field grouping (``title:(a OR b)``).
    """
    tokens = _tokenize_top_level(query)
    content: list[str] = []
    filters: list[Query] = []
    for i, tok in enumerate(tokens):
        m = _CLAUSE_RE.match(tok)
        adjacent_bool = (i > 0 and tokens[i - 1] in _BOOL_OPS) or (
            i + 1 < len(tokens) and tokens[i + 1] in _BOOL_OPS
        )
        compiled: Query | None = None
        if m is not None and not adjacent_bool:
            field, value = m.group(1), m.group(2)
            if field in ("has", "exists"):
                # Presence query. A text field: any doc with a non-empty term.
                # A numeric (u64) field: a real (non-zero) value — ``regex_query``
                # is text-only and ``.+`` would also match the 0 default, so use a
                # ``>= 1`` range (``has:page`` ⇒ paginated, ``exists:mtime`` ⇒ dated).
                target = resolve(value)
                if target is not None:
                    if target.value is FieldValue.UINT:
                        compiled = Query.range_query(
                            schema,
                            target.tantivy_field,
                            FieldType.Unsigned,
                            1,
                            None,
                            True,
                            True,
                            False,
                        )
                    else:
                        compiled = Query.regex_query(schema, target.tantivy_field, ".+")
            else:
                spec = resolve(field)
                if spec is not None:
                    compiled = _compile(spec, value, schema, index)
        if compiled is None:
            content.append(tok)
        else:
            filters.append(compiled)
    return ExtractResult(content=" ".join(content), filters=filters)


def has_unlifted_filter(query: str, schema: tantivy.Schema) -> bool:
    """True when a known field clause stays in the content, as one beside a
    boolean operator does; only the query parser can honour it there."""
    content = extract_filters(query, schema).content
    return any(
        m.group(1) in ("has", "exists") or resolve(m.group(1)) is not None
        for m in _FIELD_NAME_RE.finditer(content)
    )
