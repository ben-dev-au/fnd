"""The boolean AST tokenizer must keep `field:[lo TO hi]` ranges intact.

Regression: a Modified/Created date filter expands to `mtime:[<ts> TO <ts>]`,
and inside the prefix-wrapped content query the tokenizer split on the space
in the brackets, yielding a truncated `mtime:[<ts>` leaf that failed to parse
("invalid query syntax").
"""

from __future__ import annotations

from pathlib import Path

import tantivy

from fnd.query import Searcher
from fnd.query_ast import Term, parse_query_ast
from fnd.schema import build_schema


def _atoms(node: object) -> list[str]:
    out: list[str] = []

    def walk(n: object) -> None:
        if isinstance(n, Term):
            out.append(n.text)
        for child in getattr(n, "children", ()):  # type: ignore[arg-type]
            walk(child)

    walk(node)
    return out


def test_range_stays_one_atom() -> None:
    node = parse_query_ast("mtime:[100 TO 200] AND tree")
    atoms = _atoms(node)
    assert "mtime:[100 TO 200]" in atoms
    assert "tree" in atoms
    assert not any(a in ("TO", "mtime:[100", "200]") for a in atoms)


def test_parenthesised_range_stays_intact() -> None:
    node = parse_query_ast("(mtime:[100 TO 200]) AND (tree)")
    assert "mtime:[100 TO 200]" in _atoms(node)


def test_exclusive_brace_range_stays_one_atom() -> None:
    # Tantivy exclusive ranges use braces; they split on the inner space the
    # same way inclusive `[...]` did before the fix.
    node = parse_query_ast("mtime:{100 TO 200} AND tree")
    atoms = _atoms(node)
    assert "mtime:{100 TO 200}" in atoms
    assert not any(a in ("TO", "mtime:{100", "200}") for a in atoms)


def test_mixed_delimiter_range_stays_one_atom() -> None:
    # Half-open ranges mix delimiters (`[lo TO hi}`); the tokenizer must close
    # on whichever bracket/brace ends the range.
    assert "mtime:[100 TO 200}" in _atoms(parse_query_ast("mtime:[100 TO 200} AND tree"))
    assert "mtime:{100 TO 200]" in _atoms(parse_query_ast("mtime:{100 TO 200] AND tree"))


def _index(tmp_path: Path) -> Path:
    from fnd.schema import SCHEMA_VERSION

    idx = tmp_path / "idx"
    idx.mkdir()
    (idx / ".fnd-schema-version").write_text(str(SCHEMA_VERSION))
    index = tantivy.Index(build_schema(), path=str(idx))
    w = index.writer(15_000_000)
    for pid, mt in (("a.md", 150), ("b.md", 5)):
        d = tantivy.Document()
        d.add_text("parent_id", pid)
        d.add_text("path", pid)
        d.add_text("body", "tree")
        d.add_unsigned("mtime", mt)
        w.add_document(d)
    w.commit()
    index.reload()
    return idx


def test_range_query_runs_end_to_end(tmp_path: Path) -> None:
    """The full prefix-wrapped form the date filter produces must parse and
    actually restrict."""
    s = Searcher(index_dir=_index(tmp_path))
    hits = s.search("(mtime:[100 TO 200]) AND (tree)")
    assert {Path(h.path).name for h in hits} == {"a.md"}
