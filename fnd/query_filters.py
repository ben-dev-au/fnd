"""Lower ``field:value`` / ``c:name`` / range clauses out of a query string into
typed tantivy filter queries (hard, unscored), leaving the scored content behind.

This is the "filter context" half of the engine (Elasticsearch / Quickwit
pattern): structural and field qualifiers restrict the result set without
affecting BM25 score, while bare terms and phrases stay in the scored content
query. See dev/audits/QUERY_SYNTAX_AUDIT.md §4.

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

from fnd.query_fields import FieldSpec, FieldValue, mtime_token_range, resolve

_BOOL_OPS = frozenset({"AND", "OR", "NOT"})
# field:value head — value captured greedily (the tokenizer already kept any
# bracketed/quoted run together as one token).
_CLAUSE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.+)$", re.DOTALL)
_RANGE_RE = re.compile(r"^\[\s*(.+?)\s+TO\s+(.+?)\s*\]$", re.IGNORECASE)
_CMP_RE = re.compile(r"^(>=|<=|>|<)(.+)$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class ExtractResult:
    content: str
    filters: list[Query]


def _tokenize_top_level(s: str) -> list[str]:
    """Split ``s`` into whitespace-separated top-level tokens, keeping any
    ``"…"`` / ``'…'`` / ``[…]`` / ``(…)`` run (with its inner spaces) intact."""
    tokens: list[str] = []
    buf: list[str] = []
    depth = 0  # () or [] nesting
    quote: str | None = None
    for ch in s:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            continue
        if ch in "([":
            depth += 1
            buf.append(ch)
            continue
        if ch in ")]":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch.isspace() and depth == 0:
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
            return rng(spec.coerce(m.group(1)), spec.coerce(m.group(2)))
        m = _CMP_RE.match(value)
        if m:
            op, n = m.group(1), spec.coerce(m.group(2))
            if op == ">":
                return rng(n, None, inc_lo=False)
            if op == ">=":
                return rng(n, None)
            if op == "<":
                return rng(None, n, inc_hi=False)
            return rng(None, n)  # <=
        if spec.query_name == "mtime":
            tok = mtime_token_range(value)
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
    # Field grouping: ``title:(a OR b)`` → the boolean parsed against that field.
    # Needs the index (parse_query); without it, fall through to term/phrase.
    if index is not None and value.startswith("(") and value.endswith(")"):
        try:
            return index.parse_query(value, default_field_names=[spec.tantivy_field])
        except ValueError:
            return None
    if spec.value is FieldValue.UINT:
        return _uint_range(spec, value, schema)
    if spec.value is FieldValue.EXACT:
        if spec.query_name == "collection":
            names = [_strip_quotes(n.strip()) for n in value.split(",") if n.strip()]
            terms = [Query.term_query(schema, spec.tantivy_field, n) for n in names]
            if not terms:
                return None
            if len(terms) == 1:
                return terms[0]
            return Query.boolean_query([(tantivy.Occur.Should, t) for t in terms])
        return Query.term_query(schema, spec.tantivy_field, _strip_quotes(value).lower())
    # TEXT (default/stem tokenizer): quoted → phrase, single word → term.
    raw = _strip_quotes(value)
    words = [w.lower() for w in _WORD_RE.findall(raw)]
    if not words:
        return None
    if len(words) == 1:
        return Query.term_query(schema, spec.tantivy_field, words[0])
    return Query.phrase_query(schema, spec.tantivy_field, words)


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
