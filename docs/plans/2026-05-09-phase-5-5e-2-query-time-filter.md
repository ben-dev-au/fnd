# Phase 5.5e-2 — Query-Time Metadata Filtering Implementation Plan


**Spec:** [`docs/specs/2026-05-09-collection-crud-and-source-filters-design.md`](../specs/2026-05-09-collection-crud-and-source-filters-design.md) — sections "Schema", "Query DSL pre-pass", "Query layer", and "Saved searches & history".

**Goal:** Make the same DSL that 5.5e-1 applied at index time work at query time, by storing frontmatter in a Tantivy `meta_blob` field and post-filtering ranked hits via the same compiled predicate. Inline `[…]` syntax in the query bar plus a `--meta` CLI flag are the user-facing surfaces.

**Architecture:** One new helper module (`acorn/meta_blob.py`) handling JSON serialization with date-roundtrip. `acorn/schema.py` gains a stored `meta_blob` bytes field; the sidecar `.acorn-schema-version` jumps from 1 → 2 so old indexes refuse to load until rebuilt. `acorn/index.py` writes the encoded frontmatter for every md chunk. `acorn/query_dsl.py` gains `split_metadata_filter` to extract one `[…]` clause from a user query. `acorn/query.py:Searcher` accepts a `metadata_filter` kwarg and runs post-filter with oversample-and-retry. The TUI (`acorn/tui/app.py`) splits the user-typed string before submission and surfaces parse errors inline; the CLI (`acorn/cli.py:search`) gains `--meta`.

**Tech Stack:** Python 3.13, Tantivy (schema bytes field), stdlib `json` (custom encoder for `datetime.date`), Textual (existing notify mechanism for parse-error feedback), pytest.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `acorn/schema.py` | modify | Add `F_META_BLOB`, declare bytes field, bump `SCHEMA_VERSION` to 2 |
| `acorn/meta_blob.py` | create | `encode(fm: dict) -> bytes` and `decode(blob: bytes) -> dict` with date-aware JSON round-trip |
| `acorn/index.py` | modify | Read frontmatter once per file (md only); attach encoded blob to every chunk's document |
| `acorn/query.py` | modify | `Hit.meta_blob: bytes = b""`; `_raw_hits` populates it; `Searcher.search`/`search_grouped` accept `metadata_filter`; new `_filtered_raw_hits` does oversample-and-retry |
| `acorn/query_dsl.py` | modify | New `split_metadata_filter(query) -> tuple[str, str \| None]` |
| `acorn/cli.py` | modify | `acorn search` gains `--meta` option |
| `acorn/tui/app.py` | modify | `_run_query` splits inline `[…]`, plumbs `metadata_filter`; parse errors via `self.notify(...)` |
| `tests/test_meta_blob.py` | create | Encode/decode round-trips for primitives, lists, dates, empty |
| `tests/test_schema_meta_blob.py` | create | Schema version bump, refusal of stale sidecar |
| `tests/test_query_dsl_split.py` | create | `split_metadata_filter` cases (start/middle/end, in phrase, multiple, none) |
| `tests/test_query_metadata_filter.py` | create | End-to-end: index md with frontmatter → filter at query time → only matches survive; oversample correctness; non-md kind passthrough; bad filter raises FilterError |
| `tests/test_cli_search_meta.py` | create | `acorn search --meta` CLI behaviour |

Existing tests under `tests/` will continue to pass without changes — the new `meta_blob` field is optional in queries that don't use it. The `tmp_index_dir` fixture is per-test, so the schema-version bump rebuilds fresh per run.

---

## Conventions

- All Python files: `from __future__ import annotations` at top.
- Tests use `pytest`; per-test corpora via `tmp_path`; existing `tmp_index_dir` fixture from `tests/conftest.py`.
- Conventional Commits with §5.5e-2 reference; one commit per task.
- Pre-commit (ruff + pyright strict + pytest-fast) runs on every commit. Don't bypass.

---

## Task 1: Schema bump + `meta_blob` field

**Files:**
- Modify: `acorn/schema.py`
- Test: `tests/test_schema_meta_blob.py`

- [ ] **Step 1: Add failing test**

Create `tests/test_schema_meta_blob.py`:

```python
"""Phase 5.5e-2: schema bump and meta_blob field declaration."""

from __future__ import annotations

from pathlib import Path

import pytest
from tantivy import Document

from acorn.schema import F_META_BLOB, SCHEMA_VERSION, build_schema


def test_schema_version_bumped_to_two() -> None:
    assert SCHEMA_VERSION == 2


def test_meta_blob_field_constant_exists() -> None:
    assert F_META_BLOB == "meta_blob"


def test_schema_accepts_meta_blob_bytes() -> None:
    """The schema must accept ``meta_blob`` as a stored bytes field — the
    indexer writes JSON-encoded frontmatter there, retrieved at query time
    by the post-filter."""
    schema = build_schema()
    doc = Document()
    # Should not raise — the field is declared and accepts bytes.
    doc.add_bytes(F_META_BLOB, b'{"Course": "DPwC"}')


def test_old_index_sidecar_refuses_load(tmp_path: Path) -> None:
    """An index dir with a v1 sidecar must refuse to load under v2."""
    from acorn.index import _ensure_index

    sidecar = tmp_path / ".acorn-schema-version"
    sidecar.write_text("1")
    with pytest.raises(RuntimeError, match="schema version"):
        _ensure_index(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schema_meta_blob.py -v`
Expected: 4 failures — `F_META_BLOB` not defined, `SCHEMA_VERSION` is 1, schema doesn't accept the field.

- [ ] **Step 3: Update `acorn/schema.py`**

Open `acorn/schema.py`. Make three changes:

1. Bump `SCHEMA_VERSION` from `1` to `2`:

```python
SCHEMA_VERSION: Final[int] = 2
```

2. Add the field name constant alongside the existing `F_*` constants (after `F_CHUNK_SEQ`):

```python
F_META_BLOB: Final = "meta_blob"
```

3. Inside `build_schema()`, add a stored bytes field (place it next to `F_BODY_STRUCT`):

```python
    # JSON-encoded frontmatter for query-time metadata filter (§5.5e-2).
    sb.add_bytes_field(F_META_BLOB, stored=True, indexed=False)
```

4. Update the docstring table at the top of the file to include the new field. Find the `body_struct` row and append after it:

```
meta_blob         bytes    no        yes     no    JSON frontmatter for query-time filter
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_schema_meta_blob.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: most tests fail with `RuntimeError: schema version 1; current is 2 — rebuild`. That's the intended consequence of the bump — every test that uses an existing index dir on disk is now stale. Per-test fixtures (`tmp_index_dir`) build fresh and should be fine; let's confirm. If many tests fail, look at one — they should all fail at `_ensure_index`. The fixture writes the sidecar first time, so per-test indexes are immune.

If there's a global `default_index_dir()` cached on disk from prior runs, those can be ignored — only `pytest`'s `tmp_index_dir` fixtures matter for the test suite.

If existing tests fail for an unrelated reason: stop and investigate.

If they all pass: great, schema bump is transparent at the test level.

- [ ] **Step 6: Commit**

```bash
git add acorn/schema.py tests/test_schema_meta_blob.py
git commit -m "feat(schema): phase 5.5e-2 — bump SCHEMA_VERSION to 2 + add meta_blob field"
```

---

## Task 2: `acorn/meta_blob.py` — JSON encode/decode with date round-trip

**Files:**
- Create: `acorn/meta_blob.py`
- Test: `tests/test_meta_blob.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_meta_blob.py`:

```python
"""Phase 5.5e-2: JSON-roundtrip of frontmatter dicts for query-time filter."""

from __future__ import annotations

import datetime as dt

import pytest

from acorn.meta_blob import decode, encode


def test_empty_dict_roundtrip() -> None:
    assert decode(encode({})) == {}


def test_string_int_float_roundtrip() -> None:
    fm = {"Course": "DPwC", "priority": 3, "weight": 1.5}
    assert decode(encode(fm)) == fm


def test_bool_and_none_roundtrip() -> None:
    fm = {"archived": False, "active": True, "parent": None}
    out = decode(encode(fm))
    assert out == fm
    assert out["archived"] is False
    assert out["active"] is True
    assert out["parent"] is None


def test_list_roundtrip() -> None:
    fm = {"tags": ["course", "active"], "vals": [1, 2.5, True, None]}
    assert decode(encode(fm)) == fm


def test_date_roundtrip() -> None:
    """Dates must round-trip as ``datetime.date`` so the DSL evaluator's
    ordered comparisons (`<=`, `>=`) work — strings can't be compared
    against dates and silently fail closed."""
    fm = {"due": dt.date(2026, 6, 1)}
    out = decode(encode(fm))
    assert out == fm
    assert isinstance(out["due"], dt.date)


def test_date_inside_list_roundtrip() -> None:
    fm = {"deadlines": [dt.date(2026, 6, 1), dt.date(2026, 7, 1)]}
    out = decode(encode(fm))
    assert all(isinstance(d, dt.date) for d in out["deadlines"])


def test_decode_empty_bytes_returns_empty_dict() -> None:
    """Non-md chunks store empty bytes; decode must map this to ``{}`` so
    callers don't need to special-case the empty-file path."""
    assert decode(b"") == {}


def test_encode_returns_bytes() -> None:
    blob = encode({"x": 1})
    assert isinstance(blob, bytes)


def test_encode_unsupported_type_raises() -> None:
    """Sets / arbitrary objects aren't supported. Frontmatter only ever
    yields the JSON-friendly types we serialize, so anything else is a
    programming bug."""
    with pytest.raises(TypeError):
        encode({"weird": {1, 2, 3}})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_meta_blob.py -v`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'acorn.meta_blob'`.

- [ ] **Step 3: Create the module**

Create `acorn/meta_blob.py`:

```python
"""Frontmatter ↔ JSON bytes (§5.5e-2).

The Tantivy ``meta_blob`` stored field holds JSON-encoded frontmatter so
the query-time post-filter can apply the same DSL predicate the indexer
already uses (§5.5e-1). JSON doesn't natively round-trip ``datetime.date``,
so we wrap dates in a small typed envelope::

    encode({"due": date(2026, 6, 1)}) →
        b'{"due": {"__type__": "date", "value": "2026-06-01"}}'

The decoder restores them via a JSON ``object_hook``. The DSL evaluator
needs `dt.date` instances on both sides for ordered comparisons (the
:func:`acorn.filter_dsl._orderable` helper rejects str-vs-date), so the
round-trip is load-bearing — not just cosmetic.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

_TYPE_KEY = "__type__"


def encode(fm: dict[str, Any]) -> bytes:
    """Encode a frontmatter dict to JSON bytes. Raises TypeError for any
    value that isn't a JSON primitive, list of primitives, or
    ``datetime.date``."""
    return json.dumps(fm, default=_default).encode("utf-8")


def decode(blob: bytes) -> dict[str, Any]:
    """Decode bytes back into a frontmatter dict. Empty bytes map to an
    empty dict (the no-frontmatter case for non-md chunks)."""
    if not blob:
        return {}
    return json.loads(blob.decode("utf-8"), object_hook=_object_hook)


def _default(o: object) -> object:
    if isinstance(o, dt.date) and not isinstance(o, dt.datetime):
        return {_TYPE_KEY: "date", "value": o.isoformat()}
    raise TypeError(f"unsupported type for meta_blob: {type(o).__name__}")


def _object_hook(d: dict[str, Any]) -> object:
    t = d.get(_TYPE_KEY)
    if t == "date":
        return dt.date.fromisoformat(d["value"])
    return d
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_meta_blob.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add acorn/meta_blob.py tests/test_meta_blob.py
git commit -m "feat(meta_blob): phase 5.5e-2 — JSON encode/decode with date round-trip"
```

---

## Task 3: Indexer writes `meta_blob` per md chunk

**Files:**
- Modify: `acorn/index.py`
- Test: `tests/test_index_meta_blob.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_index_meta_blob.py`:

```python
"""Phase 5.5e-2: index pipeline serializes frontmatter into meta_blob."""

from __future__ import annotations

from pathlib import Path

from acorn.config import CollectionConfig, SourceConfig
from acorn.index import build_index_from_config
from acorn.meta_blob import decode
from acorn.query import Searcher
from acorn.schema import F_META_BLOB


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_md_chunk_carries_frontmatter_in_meta_blob(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    notes = tmp_path / "notes"
    _touch(
        notes / "a.md",
        "---\nCourse: DPwC\ntags: [course, active]\n---\n# A\nbody one\n",
    )
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="x", index_dir=tmp_index_dir)

    # Pull the doc back via Searcher and read F_META_BLOB.
    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("body", limit=5, collection="x")
    assert hits
    h = hits[0]
    fm = decode(h.meta_blob)
    assert fm == {"Course": "DPwC", "tags": ["course", "active"]}


def test_non_md_chunk_meta_blob_is_empty(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Only md files have YAML frontmatter; non-md chunks store empty
    bytes so query-time filters can short-circuit cheaply."""
    root = tmp_path / "txt"
    _touch(root / "a.txt", "this is plain text with no frontmatter")
    cc = CollectionConfig(sources=[SourceConfig(path=root, includes=["**/*.txt"])])
    build_index_from_config(config=cc, collection="x", index_dir=tmp_index_dir)

    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("plain text", limit=5, collection="x")
    assert hits
    assert hits[0].meta_blob == b""


def test_md_without_frontmatter_meta_blob_is_empty(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    notes = tmp_path / "notes"
    _touch(notes / "a.md", "# Heading\nplain markdown body\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="x", index_dir=tmp_index_dir)

    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("plain markdown", limit=5, collection="x")
    assert hits
    assert hits[0].meta_blob == b""
```

These tests reference `Hit.meta_blob` which doesn't exist yet — that gets added in Task 5. So the tests will collect-ERROR with an AttributeError. That's the right RED for *this* task — the indexer needs to write the field even before the read-side wiring lands. Confirm in step 2 the test fails on the lack of `Hit.meta_blob` rather than on `meta_blob == b""`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_index_meta_blob.py -v`
Expected: 3 failures, all from `AttributeError: 'Hit' object has no attribute 'meta_blob'`. (Hit gains the field in Task 5.)

This means we *can't* finish Task 3 in isolation cleanly — the test asserts via `Hit.meta_blob`. Two options: (a) skip these tests in Task 3 with `@pytest.mark.skip("wired in Task 5")` and remove the skip later, or (b) verify the indexer's behaviour by reading the doc directly through Tantivy in Task 3, then convert the tests to use `Hit.meta_blob` in Task 5.

Pick option (b): for Task 3, write the test to dive into the index doc store directly. Replace the test bodies with:

```python
def _meta_blob_for_first_hit(index_dir: Path, query: str) -> bytes:
    """Pull the first match for ``query`` and return its meta_blob bytes
    via the doc-store API directly (bypasses Hit, which doesn't carry
    meta_blob until Task 5)."""
    from tantivy import Index

    from acorn.schema import build_schema
    index = Index(build_schema(), path=str(index_dir))
    index.reload()
    searcher = index.searcher()
    parsed = index.parse_query(query, default_field_names=["body"])
    result = searcher.search(parsed, limit=1)
    if not result.hits:
        return b""
    _score, address = result.hits[0]
    doc = searcher.doc(address)
    val = doc.get_first(F_META_BLOB)  # type: ignore[attr-defined]
    return val if val is not None else b""
```

And rewrite each test to call `_meta_blob_for_first_hit(tmp_index_dir, "body")` etc. instead of accessing `Hit.meta_blob`. This keeps Task 3 self-contained.

Update the three tests above to use this helper. Example:

```python
def test_md_chunk_carries_frontmatter_in_meta_blob(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    notes = tmp_path / "notes"
    _touch(
        notes / "a.md",
        "---\nCourse: DPwC\ntags: [course, active]\n---\n# A\nbody one\n",
    )
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="x", index_dir=tmp_index_dir)
    blob = _meta_blob_for_first_hit(tmp_index_dir, "body")
    assert decode(blob) == {"Course": "DPwC", "tags": ["course", "active"]}
```

After this rewrite, run the tests again — they should now fail because `_doc_for_chunk` doesn't write `meta_blob` yet. That's the proper RED for Task 3.

- [ ] **Step 3: Wire the indexer to write `meta_blob`**

Open `acorn/index.py`. Two changes:

1. Import the encode helper and the schema constant at the top:

```python
from acorn.meta_blob import encode as encode_meta_blob
from acorn.schema import (
    # ... existing list ...
    F_META_BLOB,
    # ...
)
```

2. Update `_doc_for_chunk` to attach the encoded blob. Modify its signature to accept an optional `meta_blob_bytes`:

```python
def _doc_for_chunk(
    chunk: Chunk, *, collection: str, meta_blob_bytes: bytes = b""
) -> Document:
    doc = Document()
    # ... existing fields ...
    doc.add_bytes(F_BODY_STRUCT, encode_body_struct(chunk.body_struct))
    doc.add_bytes(F_META_BLOB, meta_blob_bytes)
    return doc
```

3. In `build_index` and `build_index_from_config`, read the file's frontmatter once (md only) before iterating chunks and pass the encoded blob to every chunk:

```python
def build_index_from_config(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
) -> int:
    from acorn.frontmatter import (
        FrontmatterParseError,
        read_frontmatter_from_file,
    )
    from acorn.walk import walk_sources

    index = _ensure_index(index_dir)
    writer = index.writer(heap_size=_WRITER_HEAP)
    if rebuild:
        writer.delete_documents(F_COLLECTION, collection)
        writer.commit()
    written = 0
    for path in walk_sources(sources=config.sources):
        meta_blob_bytes = b""
        if path.suffix.lower() == ".md":
            try:
                fm = read_frontmatter_from_file(path)
            except FrontmatterParseError:
                fm = None
            if fm:
                meta_blob_bytes = encode_meta_blob(fm)
        writer.delete_documents(F_PARENT_ID, _path_parent_id(path))
        for chunk in extract(path):
            writer.add_document(
                _doc_for_chunk(
                    chunk, collection=collection, meta_blob_bytes=meta_blob_bytes
                )
            )
            written += 1
            if written % _COMMIT_BATCH == 0:
                writer.commit()
    writer.commit()
    writer.wait_merging_threads()
    return written
```

(Same pattern for `build_index` if you want non-md to round-trip too. Since `build_index` is the ad-hoc CLI path, leave it pulling `meta_blob_bytes=b""` — it's not part of the multi-source flow.)

If `build_index` already calls `_doc_for_chunk` without passing `meta_blob_bytes`, the default `b""` covers it — no change needed there.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_index_meta_blob.py -v`
Expected: 3 passed.

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add acorn/index.py tests/test_index_meta_blob.py
git commit -m "feat(index): phase 5.5e-2 — write frontmatter to meta_blob per md chunk"
```

---

## Task 4: `split_metadata_filter` in `query_dsl.py`

**Files:**
- Modify: `acorn/query_dsl.py`
- Test: `tests/test_query_dsl_split.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_query_dsl_split.py`:

```python
"""Phase 5.5e-2: extract a single inline [metadata filter] clause from a query."""

from __future__ import annotations

import pytest

from acorn.query_dsl import split_metadata_filter


def test_no_brackets_returns_query_unchanged() -> None:
    assert split_metadata_filter("strategy pattern") == ("strategy pattern", None)


def test_brackets_at_start() -> None:
    q, m = split_metadata_filter("[Course == 'DPwC'] strategy pattern")
    assert q == "strategy pattern"
    assert m == "Course == 'DPwC'"


def test_brackets_at_end() -> None:
    q, m = split_metadata_filter("strategy pattern [Course == 'DPwC']")
    assert q == "strategy pattern"
    assert m == "Course == 'DPwC'"


def test_brackets_in_middle() -> None:
    q, m = split_metadata_filter("foo [Course == 'DPwC'] bar")
    assert q == "foo bar"
    assert m == "Course == 'DPwC'"


def test_brackets_inside_quoted_phrase_left_alone() -> None:
    """A phrase in the user's lexical query may legitimately contain
    square brackets (e.g. a code listing). Those must not be extracted."""
    q, m = split_metadata_filter('"foo [bar]" baz')
    assert q == '"foo [bar]" baz'
    assert m is None


def test_multiple_brackets_raises() -> None:
    """Only one filter clause per query in v1; users compose with AND/OR
    inside the single block."""
    with pytest.raises(ValueError, match="only one"):
        split_metadata_filter("[a == 1] foo [b == 2]")


def test_empty_brackets_returns_none() -> None:
    """An empty [] is a no-op, not an empty filter — surface as None so
    the search runs without metadata filtering."""
    q, m = split_metadata_filter("foo []")
    assert q == "foo"
    assert m is None


def test_whitespace_around_extracted_clause_collapsed() -> None:
    """Removing the bracketed clause shouldn't leave double-spaces in the
    lexical query."""
    q, _m = split_metadata_filter("foo  [a == 1]  bar")
    # Single space between foo and bar.
    assert q == "foo bar"


def test_unclosed_bracket_raises() -> None:
    with pytest.raises(ValueError, match="unclosed|unterminated"):
        split_metadata_filter("foo [a == 1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query_dsl_split.py -v`
Expected: collection ERROR — `split_metadata_filter` not exported.

- [ ] **Step 3: Implement `split_metadata_filter`**

Append to `acorn/query_dsl.py`:

```python
def split_metadata_filter(query: str) -> tuple[str, str | None]:
    """Extract a single top-level ``[…]`` clause from ``query``.

    Returns ``(lexical_query, metadata_filter_or_None)``. ``[…]`` blocks
    appearing inside a quoted phrase are left intact. An empty ``[]`` is
    treated as no filter (rather than an empty filter expression). Two or
    more bracketed blocks raise ``ValueError`` — users compose alternatives
    with ``AND``/``OR`` inside the single block.

    Whitespace around the extracted clause is collapsed so the resulting
    lexical query reads naturally.
    """
    in_quote: str | None = None
    bracket_start: int | None = None
    found_range: tuple[int, int] | None = None
    i = 0
    while i < len(query):
        ch = query[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_quote = ch
            i += 1
            continue
        if ch == "[":
            if bracket_start is not None:
                raise ValueError("unclosed [ before another [")
            bracket_start = i
            i += 1
            continue
        if ch == "]":
            if bracket_start is None:
                # Stray ']' is part of the lexical query — leave it.
                i += 1
                continue
            if found_range is not None:
                raise ValueError(
                    "only one inline [metadata filter] clause per query"
                )
            found_range = (bracket_start, i)
            bracket_start = None
            i += 1
            continue
        i += 1
    if bracket_start is not None:
        raise ValueError("unclosed [ in query")
    if found_range is None:
        return query, None
    start, end = found_range
    inner = query[start + 1 : end].strip()
    if not inner:
        # Empty []: drop it from the lexical, treat as "no filter".
        lex = (query[:start] + query[end + 1 :]).strip()
        lex = " ".join(lex.split())
        return lex, None
    lex = (query[:start] + query[end + 1 :]).strip()
    lex = " ".join(lex.split())
    return lex, inner
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_query_dsl_split.py -v`
Expected: 9 passed.

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add acorn/query_dsl.py tests/test_query_dsl_split.py
git commit -m "feat(query_dsl): phase 5.5e-2 — split inline [metadata filter] clause"
```

---

## Task 5: `Searcher.search` / `search_grouped` accept `metadata_filter`

**Files:**
- Modify: `acorn/query.py`
- Test: `tests/test_query_metadata_filter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_query_metadata_filter.py`:

```python
"""Phase 5.5e-2: query-time post-filter using compile_filter on meta_blob."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.config import CollectionConfig, SourceConfig
from acorn.filter_dsl import FilterError
from acorn.index import build_index_from_config
from acorn.query import Searcher


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def filter_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    _touch(
        notes / "dpwc.md",
        "---\nCourse: DPwC\nstatus: active\n---\n# A\npenguin sandwich here\n",
    )
    _touch(
        notes / "algos.md",
        "---\nCourse: Algorithms\nstatus: active\n---\n# B\npenguin sandwich also\n",
    )
    _touch(
        notes / "archived.md",
        "---\nCourse: DPwC\nstatus: archived\n---\n# C\npenguin sandwich third\n",
    )
    _touch(notes / "untagged.md", "# D\npenguin sandwich plain\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    return tmp_index_dir


def test_meta_filter_narrows_to_matching_md(filter_corpus: Path) -> None:
    s = Searcher(index_dir=filter_corpus)
    hits = s.search(
        "penguin sandwich",
        limit=10,
        collection="notes",
        metadata_filter="Course == 'DPwC' AND status != 'archived'",
    )
    paths = {Path(h.path).name for h in hits}
    assert "dpwc.md" in paths
    assert "algos.md" not in paths
    assert "archived.md" not in paths
    assert "untagged.md" not in paths  # strict null


def test_meta_filter_empty_string_is_no_filter(filter_corpus: Path) -> None:
    """Defensive: an empty filter string (post-strip) must NOT compile —
    callers should pass None for "no filter". An empty string is a bug
    on the caller side and we want a clear error."""
    s = Searcher(index_dir=filter_corpus)
    with pytest.raises(FilterError):
        s.search("penguin", limit=5, metadata_filter="")


def test_meta_filter_invalid_raises(filter_corpus: Path) -> None:
    s = Searcher(index_dir=filter_corpus)
    with pytest.raises(FilterError):
        s.search("penguin", limit=5, metadata_filter="Course ==")


def test_meta_filter_passes_through_when_none(filter_corpus: Path) -> None:
    """metadata_filter=None means no post-filter; same hits as without it."""
    s = Searcher(index_dir=filter_corpus)
    baseline = s.search("penguin sandwich", limit=10, collection="notes")
    with_none = s.search(
        "penguin sandwich", limit=10, collection="notes", metadata_filter=None
    )
    assert {h.parent_id for h in baseline} == {h.parent_id for h in with_none}


def test_meta_filter_grouped_dedup_still_one_hit_per_file(
    filter_corpus: Path,
) -> None:
    """search_grouped's per-file dedup applies AFTER the post-filter."""
    s = Searcher(index_dir=filter_corpus)
    groups = s.search_grouped(
        "penguin sandwich",
        limit=10,
        collection="notes",
        metadata_filter="Course == 'DPwC'",
    )
    paths = {Path(g.path).name for g in groups}
    # dpwc.md and archived.md both match Course == 'DPwC'; status filter
    # not applied here, so both surface.
    assert paths == {"dpwc.md", "archived.md"}


def test_meta_filter_oversample_still_returns_limit_when_filter_strict(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Build many md files, most failing the filter. The post-filter must
    oversample-and-retry until ``limit`` survivors emerge."""
    notes = tmp_path / "notes"
    # 50 notes, but only every 10th matches Course == 'DPwC'.
    for i in range(50):
        course = "DPwC" if i % 10 == 0 else "Other"
        _touch(
            notes / f"n{i:02}.md",
            f"---\nCourse: {course}\n---\n# {i}\npenguin sandwich {i}\n",
        )
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)

    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search(
        "penguin sandwich",
        limit=5,
        collection="notes",
        metadata_filter="Course == 'DPwC'",
    )
    # 5 of the 50 match the filter; we asked for limit=5 — must get all 5.
    assert len(hits) == 5
    for h in hits:
        # Sanity: every returned hit's path corresponds to a 0/10/20/30/40 file.
        idx = int(Path(h.path).stem.lstrip("n"))
        assert idx % 10 == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query_metadata_filter.py -v`
Expected: failures — `metadata_filter` kwarg not accepted, `Hit.meta_blob` missing.

- [ ] **Step 3: Add `meta_blob` to `Hit`**

In `acorn/query.py`, find the `Hit` dataclass and add the field at the end (preserving defaults for back-compat):

```python
@dataclass(slots=True, frozen=True)
class Hit:
    score: float
    parent_id: str
    path: str
    kind: str
    page: int
    slide: int
    heading_path: str
    title: str
    snippet: str
    chunk_seq: int = 0
    mtime: int = 0
    pass_index: int = 0
    # JSON-encoded frontmatter for the file (md only); empty bytes for
    # non-md or md without frontmatter. Read at search time from F_META_BLOB
    # so query-time post-filters (§5.5e-2) can decode and evaluate.
    meta_blob: bytes = b""
```

- [ ] **Step 4: Populate `meta_blob` in `_raw_hits`**

Find `_raw_hits` in `acorn/query.py`. Add the field-read call:

```python
            body_struct_bytes = doc.get_first(F_BODY_STRUCT)  # type: ignore[attr-defined]
            body_text = ""
            if body_struct_bytes is not None:
                blocks = decode_body_struct(body_struct_bytes)
                body_text = "\n".join(b.text for b in blocks)
            meta_blob_bytes = doc.get_first(F_META_BLOB)  # type: ignore[attr-defined]
            if meta_blob_bytes is None:
                meta_blob_bytes = b""
            out.append(
                Hit(
                    score=float(score),
                    # ... existing fields ...
                    chunk_seq=_first_int(doc, F_CHUNK_SEQ),
                    mtime=_first_int(doc, F_MTIME),
                    meta_blob=meta_blob_bytes,
                )
            )
```

Add `F_META_BLOB` to the schema imports at the top of `acorn/query.py`.

The same population must happen in `acorn/cascade.py:_materialize_hits` — find that file and add `meta_blob` there too. Otherwise cascade hits will have empty meta_blob and any metadata filter won't work for cascade results.

Also: `acorn/fusion.py` uses `Hit` constructor in two helpers (`_with_score`, `_with_pass_index`). Update both to forward `meta_blob=h.meta_blob` so the field doesn't reset to `b""` after fusion.

Same for `acorn/rerank.py:_replace_score` — forward `meta_blob=h.meta_blob`.

- [ ] **Step 5: Add `_filtered_raw_hits` helper**

Add to `acorn/query.py` (above the `Searcher` class, alongside `_make_snippet`):

```python
def _passes_meta_filter(hit: Hit, predicate) -> bool:  # type: ignore[no-untyped-def]
    """Apply ``predicate`` to a hit's frontmatter. Non-md hits and md
    hits with empty meta_blob bypass the filter entirely (md-only
    semantics matching :func:`acorn.walk.walk_sources`).
    """
    if hit.kind != "md":
        return True
    from acorn.meta_blob import decode

    fm = decode(hit.meta_blob)
    return predicate(fm)
```

Then add a method on `Searcher`:

```python
    def _filtered_raw_hits(
        self,
        query: str,
        *,
        target: int,
        collection: str | None,
        metadata_filter: str | None,
    ) -> list[Hit]:
        """Return at least ``target`` hits, applying the optional metadata
        filter post-Tantivy with oversample-and-retry."""
        if not metadata_filter:
            return self._raw_hits(query, limit=target, collection=collection)
        from acorn.filter_dsl import compile_filter

        predicate = compile_filter(metadata_filter)
        oversample = 1
        max_oversample = 50
        while True:
            raw = self._raw_hits(
                query, limit=target * oversample, collection=collection
            )
            survivors = [h for h in raw if _passes_meta_filter(h, predicate)]
            # Three exit conditions: enough survivors, the filter is so
            # strict no oversample will help, or we hit the cap.
            if len(survivors) >= target:
                return survivors
            if oversample >= max_oversample:
                return survivors
            if len(raw) < target * oversample:
                # Tantivy returned fewer than asked — there are no more
                # hits to oversample into.
                return survivors
            oversample *= 2
```

Important detail: `target * oversample` in the oversample loop — if `target=5`, `oversample=8` ⇒ 40 hits requested. The cap at `oversample >= 50` means we stop after roughly `target * 50` raw hits. For `target=10` and a filter excluding 90%+, we'd still pull up to 500 raw hits. Adjustable via the `max_oversample` constant.

- [ ] **Step 6: Plumb `metadata_filter` through `search` and `search_grouped`**

Replace the relevant slices of both methods to call `_filtered_raw_hits` instead of `_raw_hits`:

In `Searcher.search`:

```python
    def search(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        collection: str | None = None,
        profile: object | None = None,
        now: int | None = None,
        metadata_filter: str | None = None,
    ) -> list[Hit]:
        if not query.strip():
            return []
        raw = self._filtered_raw_hits(
            query,
            target=limit * 5,
            collection=collection,
            metadata_filter=metadata_filter,
        )
        if profile is not None:
            from acorn.rerank import RankingProfile, rerank_hits

            assert isinstance(profile, RankingProfile)
            raw = rerank_hits(raw, profile=profile, query=query, now=now)
        seen: set[str] = set()
        out: list[Hit] = []
        for h in raw:
            if h.parent_id in seen:
                continue
            seen.add(h.parent_id)
            out.append(h)
            if len(out) >= limit:
                break
        return out
```

In `Searcher.search_grouped`:

```python
    def search_grouped(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        sections_per_file: int = 5,
        collection: str | None = None,
        profile: object | None = None,
        now: int | None = None,
        metadata_filter: str | None = None,
    ) -> list[FileGroup]:
        if not query.strip():
            return []
        raw = self._filtered_raw_hits(
            query,
            target=limit * 10,
            collection=collection,
            metadata_filter=metadata_filter,
        )
        if profile is not None:
            from acorn.rerank import RankingProfile, rerank_hits

            assert isinstance(profile, RankingProfile)
            raw = rerank_hits(raw, profile=profile, query=query, now=now)
        # ... rest unchanged ...
```

(Keep the rest of each method body untouched — this is a swap of `_raw_hits(...)` → `_filtered_raw_hits(target=..., metadata_filter=...)`.)

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_query_metadata_filter.py -v`
Expected: 6 passed.

- [ ] **Step 8: Run earlier tasks' tests to confirm no regression**

Run: `uv run pytest tests/test_index_meta_blob.py tests/test_query_dsl_split.py tests/test_meta_blob.py tests/test_schema_meta_blob.py -v`
Expected: all green.

Run: `uv run pytest -q`
Expected: full suite green.

If something else broke (e.g., `cascade_search` doesn't return `meta_blob`-populated Hits): re-check Step 4. The cascade and fusion modules build their own Hit objects — those constructions need to forward `meta_blob`.

- [ ] **Step 9: Lint + types**

Run: `uv run ruff check acorn tests && uv run pyright`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add acorn/query.py acorn/cascade.py acorn/fusion.py acorn/rerank.py tests/test_query_metadata_filter.py
git commit -m "feat(query): phase 5.5e-2 — metadata_filter post-filter via compile_filter"
```

---

## Task 6: `acorn search --meta` CLI flag

**Files:**
- Modify: `acorn/cli.py:search`
- Test: `tests/test_cli_search_meta.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_search_meta.py`:

```python
"""Phase 5.5e-2: `acorn search --meta` filters at query time."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from acorn.cli import app
from acorn.config import CollectionConfig, SourceConfig
from acorn.index import build_index_from_config


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def cli_corpus(
    tmp_path: Path, tmp_index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    notes = tmp_path / "notes"
    _touch(notes / "dpwc.md", "---\nCourse: DPwC\n---\n# A\nlightning rod\n")
    _touch(notes / "other.md", "---\nCourse: Other\n---\n# B\nlightning rod\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    monkeypatch.setattr("acorn.cli.default_index_dir", lambda: tmp_index_dir)
    return tmp_index_dir


def test_search_meta_flag_filters_results(cli_corpus: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "search",
            "lightning rod",
            "--collection",
            "notes",
            "--meta",
            "Course == 'DPwC'",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dpwc.md" in result.output
    assert "other.md" not in result.output


def test_search_no_meta_returns_both(cli_corpus: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["search", "lightning rod", "--collection", "notes"]
    )
    assert result.exit_code == 0
    assert "dpwc.md" in result.output
    assert "other.md" in result.output


def test_search_meta_invalid_filter_exits_nonzero(cli_corpus: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "search",
            "lightning rod",
            "--collection",
            "notes",
            "--meta",
            "Course ==",
        ],
    )
    assert result.exit_code != 0
    assert "col" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_search_meta.py -v`
Expected: 3 failures — `--meta` not recognised.

- [ ] **Step 3: Add `--meta` to `acorn search`**

Open `acorn/cli.py`. Find the `@app.command()` `search` function. Replace it with:

```python
@app.command()
def search(
    query: str,
    limit: int = 10,
    collection: str | None = typer.Option(None, "--collection", "-c"),
    meta: str | None = typer.Option(
        None, "--meta", help="Inline metadata-filter DSL (md hits only)."
    ),
) -> None:
    """Search the index and print ranked file:locator snippets to stdout."""
    from acorn.config import default_index_dir
    from acorn.filter_dsl import FilterError
    from acorn.query import Searcher

    searcher = Searcher(index_dir=default_index_dir())
    try:
        hits = searcher.search(
            query, limit=limit, collection=collection, metadata_filter=meta
        )
    except FilterError as e:
        typer.echo(f"invalid filter: {e.message} (col {e.column})", err=True)
        raise typer.Exit(code=1) from e
    for hit in hits:
        loc = ""
        if hit.page:
            loc = f":p.{hit.page}"
        elif hit.slide:
            loc = f":s.{hit.slide}"
        elif hit.heading_path:
            loc = f" §{hit.heading_path}"
        typer.echo(f"{hit.score:6.3f}  {hit.path}{loc}\n        {hit.snippet}")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cli_search_meta.py -v`
Expected: 3 passed.

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add acorn/cli.py tests/test_cli_search_meta.py
git commit -m "feat(cli): phase 5.5e-2 — acorn search --meta filters at query time"
```

---

## Task 7: TUI plumbs inline `[…]` syntax + parse-error feedback

**Files:**
- Modify: `acorn/tui/app.py`
- Test: `tests/test_tui_metadata_filter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tui_metadata_filter.py`:

```python
"""Phase 5.5e-2: TUI extracts inline [filter] from query bar."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.config import Config, CollectionConfig, SourceConfig
from acorn.index import build_index_from_config
from acorn.tui.app import AcornApp


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def tui_corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    _touch(notes / "in.md", "---\nCourse: DPwC\n---\n# A\nblue penguin\n")
    _touch(notes / "out.md", "---\nCourse: Other\n---\n# B\nblue penguin\n")
    cc = CollectionConfig(sources=[SourceConfig(path=notes, includes=["**/*.md"])])
    build_index_from_config(config=cc, collection="notes", index_dir=tmp_index_dir)
    return tmp_index_dir


@pytest.mark.asyncio
async def test_tui_inline_filter_narrows_results(tui_corpus: Path) -> None:
    cfg = Config(
        collections={
            "notes": CollectionConfig(
                sources=[SourceConfig(path=Path("/dev/null"))]
            )
        }
    )
    app = AcornApp(
        index_dir=tui_corpus, collection="notes", config=cfg
    )
    async with app.run_test() as pilot:
        q = app.query_one("#query_bar")
        q.value = "[Course == 'DPwC'] blue penguin"  # type: ignore[attr-defined]
        await pilot.press("enter")
        await pilot.pause()
        # Expect only `in.md` in the results tree.
        groups = app._groups  # type: ignore[attr-defined]
        paths = {Path(g.path).name for g in groups}
        assert "in.md" in paths
        assert "out.md" not in paths


@pytest.mark.asyncio
async def test_tui_invalid_filter_does_not_run_search(tui_corpus: Path) -> None:
    cfg = Config(
        collections={
            "notes": CollectionConfig(
                sources=[SourceConfig(path=Path("/dev/null"))]
            )
        }
    )
    app = AcornApp(
        index_dir=tui_corpus, collection="notes", config=cfg
    )
    async with app.run_test() as pilot:
        q = app.query_one("#query_bar")
        q.value = "[Course ==] foo"  # type: ignore[attr-defined]
        await pilot.press("enter")
        await pilot.pause()
        # No groups should have populated; the search should have been skipped.
        groups = app._groups  # type: ignore[attr-defined]
        assert groups == []
```

(Adjust the `app.run_test()` and the `app._groups` access patterns to match the existing TUI test style — see `tests/test_tui.py` and `tests/test_collection_picker.py` for the prevailing patterns.)

If the TUI exposes a different way to inspect submitted-search results in tests (e.g., `app._searcher`), use that instead. The point is: confirm that with the bracketed clause typed, only matching docs surface; with an invalid clause, no search runs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui_metadata_filter.py -v`
Expected: 2 failures — TUI doesn't yet split or apply the metadata filter.

- [ ] **Step 3: Update `_run_query` in `acorn/tui/app.py`**

Find `_run_query` (around line 243). Modify the body to split + plumb the metadata filter and surface parse errors via `self.notify`:

```python
    def _run_query(self, query: str) -> None:
        if self._searcher is None:
            return
        from acorn.filter_dsl import FilterError
        from acorn.query_dsl import split_metadata_filter

        try:
            lexical, metadata_filter = split_metadata_filter(query)
        except ValueError as e:
            self.notify(str(e), severity="error", title="Filter syntax")
            return

        self._current_query = query  # save the original (with [...]) for history
        if len(self._collections) >= 2:
            scoped_query = f"c:{','.join(self._collections)} {lexical}"
            single_col = None
        else:
            scoped_query = lexical
            single_col = self._collections[0] if self._collections else None
        try:
            self._groups = self._searcher.search_grouped(
                scoped_query,
                limit=50,
                sections_per_file=10,
                collection=single_col,
                profile=self._ranking_profile,
                metadata_filter=metadata_filter,
            )
        except FilterError as e:
            self.notify(
                f"col {e.column}: {e.message}",
                severity="error",
                title="Filter syntax",
            )
            self._groups = []
            return
        # ... rest of the existing method ...
```

(Preserve everything after the `_groups = ...` assignment — the tree population, cache invalidation, etc.)

If `self.notify` isn't available in the Textual version pinned by this project (check `pyproject.toml` — `textual>=0.85`), use `self.bell()` plus the status bar instead. `notify` has been in Textual since 0.50, so should be fine.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_tui_metadata_filter.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q`
Expected: green.

If existing TUI snapshot tests break: it's likely because the new `notify` adds a UI element. Update the snapshot if intentional; otherwise diagnose.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/app.py tests/test_tui_metadata_filter.py
git commit -m "feat(tui): phase 5.5e-2 — inline [filter] syntax with notify on parse error"
```

---

## Task 8: Acceptance smoke + close-out

**Files:**
- (verification only)

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: all green; ~270 tests after this phase (250 + ~20 new).

- [ ] **Step 2: Lint + types**

Run: `uv run ruff check acorn tests && uv run ruff format --check acorn tests && uv run pyright`
Expected: ruff check clean; ruff format will still flag the two pre-existing files (test_actions_keymap.py, test_phase_5_8_scroll_to_match.py); pyright clean.

- [ ] **Step 3: Manual end-to-end smoke (optional but recommended)**

Skip if you don't have the CLI wired up locally. Otherwise:

1. Pick a directory with at least one `.md` file containing YAML frontmatter and a phrase you can search for.
2. Run:
   ```
   uv run acorn collection add demo --source <vault> --include '**/*.md'
   uv run acorn collection reindex demo --rebuild  # bumped schema needs --rebuild
   uv run acorn search "<phrase>" --collection demo
   uv run acorn search "<phrase>" --collection demo --meta "Course == '<your value>'"
   ```
3. Confirm the second invocation narrows results to notes matching the filter.
4. Try an invalid filter: `--meta "Course =="`. Confirm exit 1 with a column-aware error.
5. Launch the TUI: `uv run acorn tui --collection demo`. Type `[Course == '<value>'] <phrase>` and press Enter. Confirm results narrow.
6. Type `[Course ==] <phrase>` (invalid). Confirm a notification surfaces and no search runs.

- [ ] **Step 4: Update task tracker**

Update task #20 description to reflect 5.5e-1 and 5.5e-2 done, 5.5e-3 remaining. (Use TaskUpdate; don't push #20 to completed yet.)

- [ ] **Step 5: No close-out commit needed unless docs were touched**

If you didn't touch any docs in step 4, skip. Otherwise:

```bash
git add docs/
git commit -m "docs: phase 5.5e-2 close-out"
```

---

## Self-review notes (kept for the executor)

- **Spec coverage**:
  - `meta_blob` schema field → Task 1
  - JSON encode/decode with date round-trip → Task 2
  - Indexer writes blob per md chunk → Task 3
  - Inline `[…]` extraction → Task 4
  - Searcher post-filter with oversample → Task 5
  - CLI `--meta` → Task 6
  - TUI inline syntax + error feedback → Task 7
  - Acceptance smoke → Task 8

- **Type / name consistency**:
  - `F_META_BLOB = "meta_blob"` — used in schema.py, index.py, query.py
  - `acorn.meta_blob.encode / decode` — bytes ↔ dict
  - `Hit.meta_blob: bytes = b""` — populated in `_raw_hits`, forwarded by cascade/fusion/rerank constructors
  - `split_metadata_filter(query) -> tuple[str, str | None]` — returns `(lexical, metadata_filter_or_None)`
  - `Searcher.search(*, metadata_filter: str | None = None)` and `search_grouped` — same signature
  - `_filtered_raw_hits(*, target, collection, metadata_filter)` — new helper

- **Out of scope (deferred to 5.5e-3)**:
  - TUI Collections form / `F3` binding
  - `acorn collection rm`
  - Saved-search persistence verification (the round-trip is automatic via `split_metadata_filter`; phase 11 of the original plan adds the saved-search UI proper)

- **Schema migration note for users**: SCHEMA_VERSION 1 → 2 means existing on-disk indexes refuse to load with a clear "rebuild" message. The user runs `acorn collection reindex <name> --rebuild` for each collection. If `acorn collection reindex --all` doesn't exist yet, that's a 5.5e-3 polish item, not a 5.5e-2 task.
