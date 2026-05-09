# Phase 5.5e-1 — Collection Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** [`docs/superpowers/specs/2026-05-09-collection-crud-and-source-filters-design.md`](../specs/2026-05-09-collection-crud-and-source-filters-design.md)

**Goal:** Ship the backend for multi-source collections with per-source includes/excludes plus a YAML-frontmatter predicate DSL applied at index time, so power users can configure filtered collections via `acorn config edit` and `acorn collection add`.

**Architecture:** Two leaf modules (`frontmatter.py` parses Obsidian-style YAML frontmatter; `filter_dsl.py` parses + evaluates the predicate DSL). Both feed into an extended `config.py` (new `SourceConfig`; `CollectionConfig` accepts `[[sources]]` *or* legacy flat `roots/includes/excludes`), an extended `walk.py` (`walk_sources`), and an extended `index.py` (per-source extraction + index-time filter on `.md` files). `tomlkit` is added for comment-preserving config writes used by `acorn collection add`.

**Tech Stack:** Python 3.13, Pydantic v2, Tantivy (unchanged this phase), pytest, hypothesis (property tests), tomlkit (new dep), stdlib `tomllib`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | modify | Add `tomlkit>=0.13` to runtime deps |
| `acorn/frontmatter.py` | create | Parse Obsidian-style YAML frontmatter blocks; `FrontmatterParseError` for malformed |
| `acorn/filter_dsl.py` | create | Tokenize → parse → evaluate the predicate DSL; `FilterError` carries column + message |
| `acorn/config.py` | modify | Add `SourceConfig`; extend `CollectionConfig` to accept `[[sources]]`; normalize legacy flat shape into a single implicit source; validators surface filter parse errors; `write_config_atomically` uses `tomlkit` |
| `acorn/walk.py` | modify | New `walk_sources(*, sources)` runs the per-source filter chain; old `walk` remains for ad-hoc CLI use |
| `acorn/index.py` | modify | `build_index_from_config` walks per-source; `.md` chunks pass through the source's compiled filter before being added to the index |
| `acorn/cli.py` | modify | New `acorn collection add NAME --source PATH ...`; rewire `acorn config validate` to surface filter errors with column |
| `tests/test_frontmatter.py` | create | YAML subset coverage + error cases |
| `tests/test_filter_dsl.py` | create | Tokenizer + parser + evaluator + property tests |
| `tests/test_config_sources.py` | create | New schema + validator + legacy-shape normalization |
| `tests/test_walk_per_source.py` | create | Per-source filter chain + frontmatter post-filter |
| `tests/test_index_per_source_filter.py` | create | End-to-end build with one filtered md source + one pdf source |
| `tests/test_cli_collection_add.py` | create | CLI command writes `[[sources]]` round-trip via `tomlkit` |
| `tests/test_config_validate_filter.py` | create | `acorn config validate` reports invalid DSL with column |

---

## Conventions used in every code block

- All Python files include `from __future__ import annotations` at the top of the file when first created.
- Tests use `pytest`; fixture corpora go in `tests/fixtures/<scenario>/` only when reused; per-test corpora use `tmp_path`.
- Commit messages follow Conventional Commits with the §-section reference (`feat(filter): phase 5.5e-1 — DSL parser per §9e`).

---

## Task 1: Add `tomlkit` dependency

**Files:**
- Modify: `pyproject.toml` (the `[project] dependencies = [...]` table)

- [ ] **Step 1: Add tomlkit to runtime deps**

Open `pyproject.toml`, find the `dependencies = [...]` list, insert this new line in alphabetical order (between `snowballstemmer` and the closing `]`):

```toml
    "tomlkit>=0.13",
```

- [ ] **Step 2: Sync the lockfile**

Run: `uv sync`
Expected: prints `Resolved N packages` then `Installed 1 package` (or similar). No errors.

- [ ] **Step 3: Smoke import**

Run: `uv run python -c "import tomlkit; print(tomlkit.__version__)"`
Expected: a version string like `0.13.x` printed; no traceback.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add tomlkit for comment-preserving config writes (§5.5e-1)"
```

---

## Task 2: Frontmatter parser — parse fence detection

**Files:**
- Create: `acorn/frontmatter.py`
- Test: `tests/test_frontmatter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_frontmatter.py`:

```python
"""Phase 5.5e-1: Obsidian-style YAML frontmatter parsing."""

from __future__ import annotations

import datetime as dt

import pytest

from acorn.frontmatter import FrontmatterParseError, read_frontmatter_from_text


def test_no_frontmatter_returns_none() -> None:
    assert read_frontmatter_from_text("# Just a heading\nbody text\n") is None


def test_empty_frontmatter_block_returns_empty_dict() -> None:
    assert read_frontmatter_from_text("---\n---\nbody\n") == {}


def test_does_not_match_when_first_line_isnt_fence() -> None:
    """A leading blank line or any non-fence content disables frontmatter."""
    assert read_frontmatter_from_text("\n---\nfoo: bar\n---\n") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'acorn.frontmatter'`.

- [ ] **Step 3: Create the module skeleton**

Create `acorn/frontmatter.py`:

```python
"""Obsidian-style YAML frontmatter parser (§5.5e-1).

Hand-rolled subset because adding PyYAML for one feature isn't worth the
dep weight. Supports the shapes Obsidian / Jekyll / Hugo / MkDocs use:
flat key→scalar / quoted-string / inline list / block list / number /
ISO date / bool / null. Nested mappings, multiline strings (``|``/``>``)
and YAML anchors are out of scope — they raise FrontmatterParseError.

A document with no leading ``---\\n`` fence returns None (signals "no
frontmatter present"). An empty fenced block returns {}.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from pathlib import Path

_FENCE = re.compile(r"^(---|\.\.\.)\s*$")


class FrontmatterParseError(Exception):
    """Raised when the leading frontmatter block exists but is malformed.

    Callers in the indexer convert this to "filter doesn't match" so a
    single typo in one note can't abort an index build.
    """


def read_frontmatter_from_text(text: str) -> dict[str, object] | None:
    """Return the parsed frontmatter, ``{}`` if the block is empty, or
    ``None`` if no frontmatter fence appears at the very start of the
    document. Raises FrontmatterParseError on malformed YAML."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    # First line must be exactly ``---`` (allow trailing whitespace).
    if not _FENCE.match(lines[0]):
        return None
    # Find the matching closing fence. The opening line is ``---``; from
    # line 1 onward, look for ``---`` or ``...`` on its own.
    for i in range(1, len(lines)):
        if _FENCE.match(lines[i]):
            body_lines = lines[1:i]
            return _parse_block(body_lines)
    raise FrontmatterParseError("frontmatter block has no closing fence")


def read_frontmatter_from_file(path: Path) -> dict[str, object] | None:
    """Convenience wrapper. Returns None if the file can't be read as
    UTF-8 text — frontmatter only makes sense for text formats."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return read_frontmatter_from_text(text)


def _parse_block(lines: list[str]) -> dict[str, object]:
    """Stub for the next task. Returns {} so the empty-block test passes."""
    if not lines:
        return {}
    raise FrontmatterParseError("frontmatter parsing not yet implemented")
```

- [ ] **Step 4: Run tests to verify the three pass**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add acorn/frontmatter.py tests/test_frontmatter.py
git commit -m "feat(frontmatter): phase 5.5e-1 — fence detection (§5.5e)"
```

---

## Task 3: Frontmatter parser — scalar key/value lines

**Files:**
- Modify: `acorn/frontmatter.py:_parse_block`
- Modify: `tests/test_frontmatter.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_frontmatter.py`:

```python
def test_bare_scalar() -> None:
    out = read_frontmatter_from_text("---\nCourse: Design Patterns with C++\n---\nbody\n")
    assert out == {"Course": "Design Patterns with C++"}


def test_quoted_string_double() -> None:
    out = read_frontmatter_from_text('---\ntitle: "Final Draft"\n---\n')
    assert out == {"title": "Final Draft"}


def test_quoted_string_single() -> None:
    out = read_frontmatter_from_text("---\ntitle: 'Final Draft'\n---\n")
    assert out == {"title": "Final Draft"}


def test_integer_and_float() -> None:
    out = read_frontmatter_from_text("---\npriority: 3\nweight: 1.5\n---\n")
    assert out == {"priority": 3, "weight": 1.5}


def test_iso_date() -> None:
    out = read_frontmatter_from_text("---\ndue: 2026-06-01\n---\n")
    assert out == {"due": dt.date(2026, 6, 1)}


def test_bool_and_null() -> None:
    out = read_frontmatter_from_text(
        "---\narchived: false\nactive: true\nparent: null\nother: ~\n---\n"
    )
    assert out == {"archived": False, "active": True, "parent": None, "other": None}


def test_unsupported_nested_mapping_raises() -> None:
    with pytest.raises(FrontmatterParseError, match="nested"):
        read_frontmatter_from_text("---\nfoo:\n  bar: baz\n---\n")


def test_unsupported_anchor_raises() -> None:
    with pytest.raises(FrontmatterParseError, match="anchor|alias|unsupported"):
        read_frontmatter_from_text("---\nfoo: &x 1\nbar: *x\n---\n")


def test_invalid_line_no_colon_raises() -> None:
    with pytest.raises(FrontmatterParseError, match="line 2"):
        read_frontmatter_from_text("---\nbroken line no colon\n---\n")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: 9 failures all from `_parse_block`'s NotImplementedError stub.

- [ ] **Step 3: Implement scalar line parsing**

Replace `_parse_block` and add helpers in `acorn/frontmatter.py`:

```python
_KEY_VALUE = re.compile(r"^([A-Za-z_][\w\- ]*?)\s*:\s*(.*)$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_block(lines: list[str]) -> dict[str, object]:
    if not lines:
        return {}
    out: dict[str, object] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        # Reject indented continuation that would imply nested mapping —
        # we don't support nested structures.
        if raw and raw[0] in (" ", "\t"):
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: nested mappings are not supported"
            )
        # Blank lines inside the block are allowed; ignore.
        if not raw.strip():
            i += 1
            continue
        m = _KEY_VALUE.match(raw)
        if not m:
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: expected ``key: value``"
            )
        key = m.group(1).rstrip()
        value_text = m.group(2)
        # YAML anchors / aliases / tags — explicit reject.
        if value_text.startswith("&") or value_text.startswith("*") or value_text.startswith("!"):
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: anchors/aliases/tags are unsupported"
            )
        out[key] = _parse_scalar(value_text)
        i += 1
    return out


def _parse_scalar(text: str) -> object:
    """Coerce one bare value into an int/float/date/bool/None/str.

    List parsing (inline ``[a, b]`` and block lists) is added in the next
    task; for now any ``[`` or block-list marker is treated as a string.
    """
    s = text.strip()
    if not s:
        return ""
    # Quoted strings.
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    # Booleans and null.
    lower = s.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    # ISO date.
    if _ISO_DATE.match(s):
        return dt.date.fromisoformat(s)
    # Number.
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    # Fallback: bare string.
    return s
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: 12 passed (3 original + 9 new).

- [ ] **Step 5: Commit**

```bash
git add acorn/frontmatter.py tests/test_frontmatter.py
git commit -m "feat(frontmatter): phase 5.5e-1 — scalar key/value lines"
```

---

## Task 4: Frontmatter parser — list values

**Files:**
- Modify: `acorn/frontmatter.py`
- Modify: `tests/test_frontmatter.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_frontmatter.py`:

```python
def test_inline_list() -> None:
    out = read_frontmatter_from_text("---\ntags: [course, active, dpwc]\n---\n")
    assert out == {"tags": ["course", "active", "dpwc"]}


def test_inline_list_quoted_items() -> None:
    out = read_frontmatter_from_text('---\ntags: ["with space", \'one\', plain]\n---\n')
    assert out == {"tags": ["with space", "one", "plain"]}


def test_inline_list_mixed_types() -> None:
    out = read_frontmatter_from_text("---\nvals: [1, 2.5, true, null]\n---\n")
    assert out == {"vals": [1, 2.5, True, None]}


def test_block_list() -> None:
    out = read_frontmatter_from_text(
        "---\ntags:\n  - course\n  - active\n  - dpwc\n---\n"
    )
    assert out == {"tags": ["course", "active", "dpwc"]}


def test_empty_inline_list() -> None:
    out = read_frontmatter_from_text("---\ntags: []\n---\n")
    assert out == {"tags": []}


def test_unterminated_inline_list_raises() -> None:
    with pytest.raises(FrontmatterParseError, match="list"):
        read_frontmatter_from_text("---\ntags: [course, active\n---\n")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: 6 new failures (existing 12 still pass).

- [ ] **Step 3: Extend parser for lists**

In `acorn/frontmatter.py`, replace `_parse_block` and `_parse_scalar`, and add list helpers:

```python
def _parse_block(lines: list[str]) -> dict[str, object]:
    if not lines:
        return {}
    out: dict[str, object] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        if raw and raw[0] in (" ", "\t"):
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: nested mappings are not supported"
            )
        if not raw.strip():
            i += 1
            continue
        m = _KEY_VALUE.match(raw)
        if not m:
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: expected ``key: value``"
            )
        key = m.group(1).rstrip()
        value_text = m.group(2)
        if value_text.startswith("&") or value_text.startswith("*") or value_text.startswith("!"):
            raise FrontmatterParseError(
                f"frontmatter line {i + 2}: anchors/aliases/tags are unsupported"
            )
        # Block list: empty value, then ``  - item`` lines.
        if value_text.strip() == "" and i + 1 < len(lines) and lines[i + 1].startswith("- "):
            j = i + 1
            items: list[object] = []
            while j < len(lines) and lines[j].startswith("- "):
                items.append(_parse_scalar(lines[j][2:]))
                j += 1
            out[key] = items
            i = j
            continue
        # Indented block list: ``  - item`` items under the key. We already
        # rejected indented continuation above, so loosen for the pattern
        # ``key:\n  - item\n  - item``.
        if value_text.strip() == "" and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("- "):
            indent = len(lines[i + 1]) - len(lines[i + 1].lstrip())
            j = i + 1
            items_indented: list[object] = []
            while j < len(lines) and lines[j].startswith(" " * indent + "- "):
                items_indented.append(_parse_scalar(lines[j][indent + 2:]))
                j += 1
            out[key] = items_indented
            i = j
            continue
        out[key] = _parse_scalar(value_text)
        i += 1
    return out


def _parse_scalar(text: str) -> object:
    s = text.strip()
    if not s:
        return ""
    # Inline list: [a, b, c]
    if s.startswith("["):
        if not s.endswith("]"):
            raise FrontmatterParseError("inline list missing closing ]")
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_csv(inner)]
    # Quoted strings.
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    # Booleans and null.
    lower = s.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    # ISO date.
    if _ISO_DATE.match(s):
        return dt.date.fromisoformat(s)
    # Number.
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    return s


def _split_csv(text: str) -> list[str]:
    """Split on commas while respecting quoted strings."""
    parts: list[str] = []
    buf = ""
    quote: str | None = None
    for ch in text:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf += ch
            continue
        if ch == ",":
            parts.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts
```

The two block-list branches look duplicated; the first handles `- item` flush-left (rare) and the second handles indented `  - item` (Obsidian default). The duplication is intentional — they have different indent rules and merging them would obscure intent.

Be sure to delete the old single-implementation versions of `_parse_block` and `_parse_scalar` from Task 3 — your new ones replace them.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_frontmatter.py -v`
Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add acorn/frontmatter.py tests/test_frontmatter.py
git commit -m "feat(frontmatter): phase 5.5e-1 — inline + block list values"
```

---

## Task 5: Filter DSL — tokenizer

**Files:**
- Create: `acorn/filter_dsl.py`
- Test: `tests/test_filter_dsl.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_filter_dsl.py`:

```python
"""Phase 5.5e-1: predicate DSL parser + evaluator."""

from __future__ import annotations

import datetime as dt

import pytest

from acorn.filter_dsl import FilterError, Token, TokenKind, tokenize


def _kinds(text: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(text)]


def test_tokenize_simple_equality() -> None:
    toks = tokenize("Course == 'DPwC'")
    assert [(t.kind, t.value) for t in toks] == [
        (TokenKind.IDENT, "Course"),
        (TokenKind.OP, "=="),
        (TokenKind.STRING, "DPwC"),
        (TokenKind.EOF, ""),
    ]


def test_tokenize_keywords_case_insensitive() -> None:
    assert _kinds("a AND b or NOT c") == [
        TokenKind.IDENT, TokenKind.AND,
        TokenKind.IDENT, TokenKind.OR,
        TokenKind.NOT, TokenKind.IDENT,
        TokenKind.EOF,
    ]


def test_tokenize_all_operators() -> None:
    toks = tokenize("== != < > <= >= ~~")
    assert [t.value for t in toks if t.kind == TokenKind.OP] == [
        "==", "!=", "<", ">", "<=", ">=", "~~",
    ]


def test_tokenize_numbers_and_dates() -> None:
    toks = tokenize("priority >= 3 AND due <= 2026-06-01")
    values = [t.value for t in toks if t.kind in (TokenKind.NUMBER, TokenKind.DATE)]
    assert values == [3, dt.date(2026, 6, 1)]


def test_tokenize_in_and_not_in() -> None:
    assert _kinds("'x' in tags") == [
        TokenKind.STRING, TokenKind.IN, TokenKind.IDENT, TokenKind.EOF,
    ]
    assert _kinds("'x' not in tags") == [
        TokenKind.STRING, TokenKind.NOT_IN, TokenKind.IDENT, TokenKind.EOF,
    ]


def test_tokenize_quoted_identifier() -> None:
    toks = tokenize('"due date" <= 2026-06-01')
    assert toks[0].kind == TokenKind.IDENT
    assert toks[0].value == "due date"


def test_tokenize_unterminated_string_raises_with_column() -> None:
    with pytest.raises(FilterError) as exc:
        tokenize("Course == 'DPwC")
    assert "unterminated" in exc.value.message.lower()
    assert exc.value.column == 11  # column of the opening quote (1-based)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_filter_dsl.py -v`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'acorn.filter_dsl'`.

- [ ] **Step 3: Implement the tokenizer**

Create `acorn/filter_dsl.py`:

```python
"""Predicate DSL parser + evaluator (§5.5e-1).

Grammar::

    expr        ::= or_expr
    or_expr     ::= and_expr ( OR and_expr )*
    and_expr    ::= not_expr ( AND not_expr )*
    not_expr    ::= NOT? atom
    atom        ::= "(" expr ")" | comparison
    comparison  ::= ident OP value
                  | value "in" ident
                  | value "not in" ident
    OP          ::= "==" | "!=" | "<" | ">" | "<=" | ">=" | "~~"
    value       ::= 'string' | "string" | number | iso_date | true | false | null
    ident       ::= word | "quoted word"

Same DSL is reused at query time (phase 5.5e-2) — the evaluator is
purely functional, takes a frontmatter dict, returns bool.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import Enum, auto


class TokenKind(Enum):
    IDENT = auto()
    STRING = auto()
    NUMBER = auto()
    DATE = auto()
    OP = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    IN = auto()
    NOT_IN = auto()
    TRUE = auto()
    FALSE = auto()
    NULL = auto()
    LPAREN = auto()
    RPAREN = auto()
    EOF = auto()


@dataclass(slots=True, frozen=True)
class Token:
    kind: TokenKind
    value: object
    column: int  # 1-based column of the token's start


class FilterError(Exception):
    """Parse-time error with 1-based column + message. Used by both the
    config validator (where it converts to ValidationError) and the TUI
    form (where it surfaces inline as the user types)."""

    def __init__(self, message: str, column: int) -> None:
        super().__init__(f"col {column}: {message}")
        self.message = message
        self.column = column


_KEYWORDS = {
    "and": TokenKind.AND,
    "or": TokenKind.OR,
    "not": TokenKind.NOT,
    "in": TokenKind.IN,
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
    "null": TokenKind.NULL,
}


# Order matters: longer operators first so ``<=`` doesn't get tokenised
# as ``<`` then ``=``.
_OPERATORS = ("==", "!=", "<=", ">=", "~~", "<", ">")


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER_RE = re.compile(r"\d+(\.\d+)?")
_BARE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")


def tokenize(text: str) -> list[Token]:
    """Return the token stream ending with an EOF token. Raises FilterError
    on unterminated strings or unrecognised characters."""
    out: list[Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        col = i + 1
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            out.append(Token(TokenKind.LPAREN, "(", col))
            i += 1
            continue
        if ch == ")":
            out.append(Token(TokenKind.RPAREN, ")", col))
            i += 1
            continue
        # Operators (longest match first).
        matched_op = next((op for op in _OPERATORS if text.startswith(op, i)), None)
        if matched_op is not None:
            out.append(Token(TokenKind.OP, matched_op, col))
            i += len(matched_op)
            continue
        # Quoted strings (' or ") — both also serve as quoted-identifier markers.
        if ch in ('"', "'"):
            close = text.find(ch, i + 1)
            if close == -1:
                raise FilterError("unterminated string", col)
            inner = text[i + 1 : close]
            # Quoted identifier vs string literal: disambiguate at the parser
            # level. Tokenize as IDENT when it looks like a multi-word key
            # (heuristic: the previous token isn't an OP or IN/NOT_IN). The
            # parser will assert if needed.
            prev = out[-1].kind if out else None
            kind = (
                TokenKind.IDENT
                if prev not in (TokenKind.OP, TokenKind.IN, TokenKind.NOT_IN)
                else TokenKind.STRING
            )
            out.append(Token(kind, inner, col))
            i = close + 1
            continue
        # Date literal (must precede number — same leading digits).
        date_match = _DATE_RE.match(text, i)
        if date_match:
            iso = date_match.group(0)
            try:
                value = dt.date.fromisoformat(iso)
            except ValueError as e:
                raise FilterError(f"invalid date {iso!r}", col) from e
            out.append(Token(TokenKind.DATE, value, col))
            i = date_match.end()
            continue
        num_match = _NUMBER_RE.match(text, i)
        if num_match:
            raw = num_match.group(0)
            value = float(raw) if "." in raw else int(raw)
            out.append(Token(TokenKind.NUMBER, value, col))
            i = num_match.end()
            continue
        ident_match = _BARE_IDENT_RE.match(text, i)
        if ident_match:
            raw = ident_match.group(0)
            kw = _KEYWORDS.get(raw.lower())
            if kw is TokenKind.NOT and _peek_in(text, ident_match.end()):
                # ``not in`` collapses to NOT_IN; consume the ``in`` keyword.
                i = _consume_in_after_not(text, ident_match.end())
                out.append(Token(TokenKind.NOT_IN, "not in", col))
                continue
            if kw is not None:
                out.append(Token(kw, raw.lower(), col))
            else:
                out.append(Token(TokenKind.IDENT, raw, col))
            i = ident_match.end()
            continue
        raise FilterError(f"unexpected character {ch!r}", col)
    out.append(Token(TokenKind.EOF, "", n + 1))
    return out


def _peek_in(text: str, pos: int) -> bool:
    """True if the next non-whitespace token at ``pos`` is the keyword ``in``."""
    while pos < len(text) and text[pos].isspace():
        pos += 1
    m = _BARE_IDENT_RE.match(text, pos)
    return m is not None and m.group(0).lower() == "in"


def _consume_in_after_not(text: str, pos: int) -> int:
    """Skip whitespace and the ``in`` keyword, return the new index."""
    while pos < len(text) and text[pos].isspace():
        pos += 1
    m = _BARE_IDENT_RE.match(text, pos)
    assert m is not None and m.group(0).lower() == "in"  # _peek_in already checked
    return m.end()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_filter_dsl.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add acorn/filter_dsl.py tests/test_filter_dsl.py
git commit -m "feat(filter): phase 5.5e-1 — DSL tokenizer with column tracking"
```

---

## Task 6: Filter DSL — parser (AST)

**Files:**
- Modify: `acorn/filter_dsl.py`
- Modify: `tests/test_filter_dsl.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_filter_dsl.py`:

```python
from acorn.filter_dsl import And, Compare, In, Not, Or, parse


def test_parse_simple_compare() -> None:
    tree = parse("Course == 'DPwC'")
    assert tree == Compare("Course", "==", "DPwC")


def test_parse_and_or_precedence() -> None:
    """AND binds tighter than OR (matches typical predicate languages)."""
    tree = parse("a == 1 OR b == 2 AND c == 3")
    # Expected: a == 1 OR (b == 2 AND c == 3)
    assert tree == Or(
        Compare("a", "==", 1),
        And(Compare("b", "==", 2), Compare("c", "==", 3)),
    )


def test_parse_parens_override_precedence() -> None:
    tree = parse("(a == 1 OR b == 2) AND c == 3")
    assert tree == And(
        Or(Compare("a", "==", 1), Compare("b", "==", 2)),
        Compare("c", "==", 3),
    )


def test_parse_not() -> None:
    tree = parse("NOT a == 1")
    assert tree == Not(Compare("a", "==", 1))


def test_parse_in_membership() -> None:
    tree = parse("'course' in tags")
    assert tree == In("course", "tags", negated=False)


def test_parse_not_in() -> None:
    tree = parse("'archived' not in tags")
    assert tree == In("archived", "tags", negated=True)


def test_parse_quoted_identifier_with_space() -> None:
    tree = parse('"due date" <= 2026-06-01')
    assert tree == Compare("due date", "<=", dt.date(2026, 6, 1))


def test_parse_empty_raises() -> None:
    with pytest.raises(FilterError, match="empty|expected"):
        parse("")


def test_parse_dangling_operator_raises_with_column() -> None:
    with pytest.raises(FilterError) as exc:
        parse("Course ==")
    assert exc.value.column >= 9


def test_parse_unmatched_paren_raises() -> None:
    with pytest.raises(FilterError, match="paren|expected"):
        parse("(a == 1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_filter_dsl.py -v`
Expected: 10 new failures (existing 7 still pass) — `parse` not exported, AST nodes not defined.

- [ ] **Step 3: Implement parser + AST**

Append to `acorn/filter_dsl.py`:

```python
# ── AST nodes ─────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Compare:
    """A field-vs-value comparison: ``Course == 'DPwC'``."""

    field: str
    op: str  # one of ==, !=, <, >, <=, >=, ~~
    value: object


@dataclass(slots=True, frozen=True)
class In:
    """Membership test: ``'course' in tags``. ``negated=True`` for ``not in``."""

    value: object
    field: str
    negated: bool


@dataclass(slots=True, frozen=True)
class And:
    left: object
    right: object


@dataclass(slots=True, frozen=True)
class Or:
    left: object
    right: object


@dataclass(slots=True, frozen=True)
class Not:
    operand: object


# ── Recursive-descent parser ──────────────────────────────────────


def parse(text: str) -> object:
    """Tokenize + parse into an AST. Raises FilterError on syntax issues
    with a 1-based column."""
    if not text.strip():
        raise FilterError("empty filter expression", 1)
    tokens = tokenize(text)
    parser = _Parser(tokens)
    tree = parser.parse_or()
    if parser.peek().kind is not TokenKind.EOF:
        raise FilterError(
            f"unexpected token {parser.peek().value!r}", parser.peek().column
        )
    return tree


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def peek(self) -> Token:
        return self._tokens[self._pos]

    def advance(self) -> Token:
        t = self._tokens[self._pos]
        self._pos += 1
        return t

    def expect(self, kind: TokenKind) -> Token:
        t = self.peek()
        if t.kind is not kind:
            raise FilterError(f"expected {kind.name}, got {t.value!r}", t.column)
        return self.advance()

    # or_expr ::= and_expr ( OR and_expr )*
    def parse_or(self) -> object:
        left = self.parse_and()
        while self.peek().kind is TokenKind.OR:
            self.advance()
            right = self.parse_and()
            left = Or(left, right)
        return left

    # and_expr ::= not_expr ( AND not_expr )*
    def parse_and(self) -> object:
        left = self.parse_not()
        while self.peek().kind is TokenKind.AND:
            self.advance()
            right = self.parse_not()
            left = And(left, right)
        return left

    # not_expr ::= NOT? atom
    def parse_not(self) -> object:
        if self.peek().kind is TokenKind.NOT:
            self.advance()
            return Not(self.parse_atom())
        return self.parse_atom()

    # atom ::= "(" expr ")" | comparison
    def parse_atom(self) -> object:
        t = self.peek()
        if t.kind is TokenKind.LPAREN:
            self.advance()
            inner = self.parse_or()
            close = self.peek()
            if close.kind is not TokenKind.RPAREN:
                raise FilterError("expected closing paren )", close.column)
            self.advance()
            return inner
        return self.parse_comparison()

    def parse_comparison(self) -> object:
        first = self.peek()
        # Form A: ident OP value
        if first.kind is TokenKind.IDENT:
            self.advance()
            op_tok = self.peek()
            if op_tok.kind is TokenKind.OP:
                self.advance()
                value = self._parse_value()
                return Compare(str(first.value), op_tok.value, value)
            # Form B: ident is the LHS of an "in"/"not in" — but that's
            # Form C below. Re-raise with the actual context.
            raise FilterError(
                f"expected operator after {first.value!r}", op_tok.column
            )
        # Form C: value ("in"|"not in") ident
        if first.kind in (
            TokenKind.STRING, TokenKind.NUMBER, TokenKind.DATE,
            TokenKind.TRUE, TokenKind.FALSE, TokenKind.NULL,
        ):
            value = self._parse_value()
            mem = self.peek()
            if mem.kind is TokenKind.IN:
                self.advance()
                ident = self.expect(TokenKind.IDENT)
                return In(value, str(ident.value), negated=False)
            if mem.kind is TokenKind.NOT_IN:
                self.advance()
                ident = self.expect(TokenKind.IDENT)
                return In(value, str(ident.value), negated=True)
            raise FilterError(
                f"expected 'in' / 'not in' after value", mem.column
            )
        raise FilterError(f"unexpected token {first.value!r}", first.column)

    def _parse_value(self) -> object:
        t = self.advance()
        if t.kind is TokenKind.STRING:
            return t.value
        if t.kind is TokenKind.NUMBER:
            return t.value
        if t.kind is TokenKind.DATE:
            return t.value
        if t.kind is TokenKind.TRUE:
            return True
        if t.kind is TokenKind.FALSE:
            return False
        if t.kind is TokenKind.NULL:
            return None
        if t.kind is TokenKind.IDENT:
            # Bare identifier on the value side is an error — values must be
            # quoted strings, numbers, dates, or keywords.
            raise FilterError(
                f"expected value, got identifier {t.value!r}; quote string values",
                t.column,
            )
        raise FilterError(f"expected value, got {t.value!r}", t.column)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_filter_dsl.py -v`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add acorn/filter_dsl.py tests/test_filter_dsl.py
git commit -m "feat(filter): phase 5.5e-1 — DSL parser + AST"
```

---

## Task 7: Filter DSL — evaluator + public compile_filter API

**Files:**
- Modify: `acorn/filter_dsl.py`
- Modify: `tests/test_filter_dsl.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_filter_dsl.py`:

```python
from acorn.filter_dsl import compile_filter, parse_or_error


def test_eval_equality_match() -> None:
    pred = compile_filter("Course == 'DPwC'")
    assert pred({"Course": "DPwC"}) is True
    assert pred({"Course": "Other"}) is False


def test_eval_inequality() -> None:
    pred = compile_filter("status != 'archived'")
    assert pred({"status": "active"}) is True
    assert pred({"status": "archived"}) is False


def test_eval_missing_field_strict_null() -> None:
    """Per spec: missing field treats the predicate as False — even for !=
    and even for `not in`. The user opted into strict null."""
    pred_eq = compile_filter("Course == 'DPwC'")
    pred_neq = compile_filter("Course != 'DPwC'")
    pred_in = compile_filter("'x' in tags")
    pred_not_in = compile_filter("'x' not in tags")
    empty: dict[str, object] = {}
    assert pred_eq(empty) is False
    assert pred_neq(empty) is False
    assert pred_in(empty) is False
    assert pred_not_in(empty) is False


def test_eval_numeric_compare() -> None:
    pred = compile_filter("priority >= 3")
    assert pred({"priority": 3}) is True
    assert pred({"priority": 5}) is True
    assert pred({"priority": 2}) is False


def test_eval_date_compare() -> None:
    pred = compile_filter("due <= 2026-06-01")
    assert pred({"due": dt.date(2026, 5, 30)}) is True
    assert pred({"due": dt.date(2026, 6, 2)}) is False


def test_eval_type_mismatch_returns_false() -> None:
    """String < number doesn't crash; it's just False."""
    pred = compile_filter("Course < 5")
    assert pred({"Course": "DPwC"}) is False


def test_eval_glob_match() -> None:
    pred = compile_filter("Course ~~ 'Design *'")
    assert pred({"Course": "Design Patterns"}) is True
    assert pred({"Course": "Algorithms"}) is False


def test_eval_in_list() -> None:
    pred = compile_filter("'course' in tags")
    assert pred({"tags": ["course", "active"]}) is True
    assert pred({"tags": ["something", "else"]}) is False


def test_eval_and_or_not() -> None:
    pred = compile_filter(
        "Course == 'DPwC' AND status != 'archived' AND 'active' in tags"
    )
    assert pred({"Course": "DPwC", "status": "active", "tags": ["active"]}) is True
    assert pred({"Course": "DPwC", "status": "archived", "tags": ["active"]}) is False


def test_parse_or_error_returns_predicate_for_valid() -> None:
    pred, err = parse_or_error("Course == 'DPwC'")
    assert err is None
    assert pred is not None and pred({"Course": "DPwC"}) is True


def test_parse_or_error_returns_error_for_invalid() -> None:
    pred, err = parse_or_error("Course ==")
    assert pred is None
    assert err is not None
    assert err.column >= 9


def test_compile_filter_invalid_raises() -> None:
    with pytest.raises(FilterError):
        compile_filter("not valid syntax!")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_filter_dsl.py -v`
Expected: 12 new failures.

- [ ] **Step 3: Implement evaluator + public API**

Append to `acorn/filter_dsl.py`:

```python
# ── Evaluator ─────────────────────────────────────────────────────


import fnmatch
from collections.abc import Callable, Mapping

Predicate = Callable[[Mapping[str, object]], bool]


def compile_filter(text: str) -> Predicate:
    """Parse ``text`` into a callable predicate. Raises FilterError on
    syntax issues. The returned predicate is pure: it never raises and
    returns False on type mismatches or missing fields (strict null)."""
    tree = parse(text)
    return _make_evaluator(tree)


def parse_or_error(text: str) -> tuple[Predicate | None, FilterError | None]:
    """Same as :func:`compile_filter` but never raises — returns either a
    usable predicate or a structured error. Used by the TUI form so the
    user gets inline syntax feedback as they type."""
    if not text.strip():
        return None, None  # empty filter is valid: no predicate, no error
    try:
        return compile_filter(text), None
    except FilterError as e:
        return None, e


def _make_evaluator(node: object) -> Predicate:
    if isinstance(node, And):
        left = _make_evaluator(node.left)
        right = _make_evaluator(node.right)
        return lambda fm: left(fm) and right(fm)
    if isinstance(node, Or):
        left = _make_evaluator(node.left)
        right = _make_evaluator(node.right)
        return lambda fm: left(fm) or right(fm)
    if isinstance(node, Not):
        inner = _make_evaluator(node.operand)
        return lambda fm: not inner(fm)
    if isinstance(node, Compare):
        field, op, value = node.field, node.op, node.value
        return lambda fm: _eval_compare(fm, field, op, value)
    if isinstance(node, In):
        return lambda fm: _eval_in(fm, node.value, node.field, node.negated)
    raise AssertionError(f"unknown AST node {type(node).__name__}")


def _eval_compare(fm: Mapping[str, object], field: str, op: str, value: object) -> bool:
    if field not in fm:
        # Strict null: missing field is False for every comparison.
        return False
    actual = fm[field]
    if op == "==":
        return actual == value
    if op == "!=":
        return actual != value
    if op == "~~":
        if not isinstance(actual, str) or not isinstance(value, str):
            return False
        return fnmatch.fnmatchcase(actual, value)
    # Ordered compares: numeric–numeric or date–date only.
    if op in ("<", ">", "<=", ">="):
        if not _orderable(actual, value):
            return False
        if op == "<":
            return actual < value  # type: ignore[operator]
        if op == ">":
            return actual > value  # type: ignore[operator]
        if op == "<=":
            return actual <= value  # type: ignore[operator]
        if op == ">=":
            return actual >= value  # type: ignore[operator]
    return False


def _eval_in(
    fm: Mapping[str, object], value: object, field: str, negated: bool
) -> bool:
    if field not in fm:
        return False  # strict null even for `not in`
    container = fm[field]
    if not isinstance(container, (list, tuple)):
        return False
    is_member = value in container
    return (not is_member) if negated else is_member


def _orderable(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return False  # bool is a subtype of int — explicitly reject
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return True
    if isinstance(a, dt.date) and isinstance(b, dt.date):
        return True
    return False
```

The `import fnmatch` and `from collections.abc import Callable, Mapping` lines belong at the top of the file alongside the existing imports — move them up there in your edit, don't leave them mid-file.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_filter_dsl.py -v`
Expected: 29 passed.

- [ ] **Step 5: Add hypothesis property test**

Append to `tests/test_filter_dsl.py`:

```python
from hypothesis import given
from hypothesis import strategies as st


@st.composite
def _fields_and_values(draw: st.DrawFn) -> tuple[str, object]:
    field = draw(st.sampled_from(["a", "b", "c", "Course", "status"]))
    value = draw(st.one_of(
        st.text(min_size=1, max_size=10).filter(lambda s: "'" not in s and '"' not in s),
        st.integers(min_value=-100, max_value=100),
    ))
    return field, value


@given(_fields_and_values())
def test_property_equality_then_inequality_partition(
    sample: tuple[str, object],
) -> None:
    """For any field/value, eq(fm) XOR neq(fm) is True when the field is
    present (strict null exempts the missing-field case)."""
    field, value = sample
    if isinstance(value, str):
        lit = f"'{value}'"
    else:
        lit = str(value)
    pred_eq = compile_filter(f"{field} == {lit}")
    pred_neq = compile_filter(f"{field} != {lit}")
    fm = {field: value}
    assert pred_eq(fm) ^ pred_neq(fm) or (pred_eq(fm) is False and pred_neq(fm) is False)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_filter_dsl.py -v`
Expected: 30 passed.

- [ ] **Step 7: Commit**

```bash
git add acorn/filter_dsl.py tests/test_filter_dsl.py
git commit -m "feat(filter): phase 5.5e-1 — DSL evaluator with strict null + property test"
```

---

## Task 8: Extend `CollectionConfig` with `[[sources]]`

**Files:**
- Modify: `acorn/config.py`
- Test: `tests/test_config_sources.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_config_sources.py`:

```python
"""Phase 5.5e-1: SourceConfig + multi-source collection schema."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from acorn.config import Config, SourceConfig, load


def _write_config(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_new_sources_shape_loads(tmp_path: Path) -> None:
    p = _write_config(tmp_path / "c.toml", """
        [[collections.coursework.sources]]
        path     = "~/Notes"
        includes = ["**/*.md"]
        excludes = ["**/.trash/**"]
        frontmatter_filter = "Course == 'DPwC'"

        [[collections.coursework.sources]]
        path     = "~/Course/DPwC"
        includes = ["**/*.pdf"]
    """)
    cfg = load(p)
    coursework = cfg.collection("coursework")
    assert len(coursework.sources) == 2
    assert isinstance(coursework.sources[0], SourceConfig)
    assert coursework.sources[0].includes == ["**/*.md"]
    assert coursework.sources[0].frontmatter_filter == "Course == 'DPwC'"
    assert coursework.sources[1].includes == ["**/*.pdf"]
    assert coursework.sources[1].frontmatter_filter is None


def test_legacy_flat_shape_normalised_to_one_source(tmp_path: Path) -> None:
    """The old `roots = [...]` shape still loads; loader rewrites it as a
    single implicit source with no frontmatter_filter."""
    p = _write_config(tmp_path / "c.toml", """
        [collections.papers]
        roots    = ["~/Documents/Papers"]
        includes = ["**/*.pdf"]
        excludes = ["**/Archive/**"]
    """)
    cfg = load(p)
    papers = cfg.collection("papers")
    assert len(papers.sources) == 1
    s = papers.sources[0]
    assert s.path == Path("~/Documents/Papers").expanduser()
    assert s.includes == ["**/*.pdf"]
    assert s.excludes == ["**/Archive/**"]
    assert s.frontmatter_filter is None


def test_mixing_sources_and_roots_raises(tmp_path: Path) -> None:
    p = _write_config(tmp_path / "c.toml", """
        [collections.bad]
        roots = ["~/x"]
        [[collections.bad.sources]]
        path = "~/y"
    """)
    with pytest.raises(ValidationError, match="mixes legacy 'roots' with 'sources'"):
        load(p)


def test_invalid_filter_dsl_raises_at_load(tmp_path: Path) -> None:
    p = _write_config(tmp_path / "c.toml", """
        [[collections.x.sources]]
        path = "~/x"
        frontmatter_filter = "Course =="
    """)
    with pytest.raises(ValidationError) as exc:
        load(p)
    msg = str(exc.value)
    assert "frontmatter_filter" in msg
    assert "col" in msg


def test_paths_tilde_expanded(tmp_path: Path) -> None:
    p = _write_config(tmp_path / "c.toml", """
        [[collections.x.sources]]
        path = "~/Notes"
    """)
    cfg = load(p)
    s = cfg.collection("x").sources[0]
    assert "~" not in str(s.path)


def test_default_includes_excludes_empty_when_omitted(tmp_path: Path) -> None:
    p = _write_config(tmp_path / "c.toml", """
        [[collections.x.sources]]
        path = "~/x"
    """)
    s = load(p).collection("x").sources[0]
    assert s.includes == []
    assert s.excludes == []
    assert s.follow_symlinks is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_sources.py -v`
Expected: 6 failures — `SourceConfig` not exported; `CollectionConfig.sources` doesn't exist.

- [ ] **Step 3: Extend the config schema**

Open `acorn/config.py`. Add `SourceConfig` near `CollectionConfig` and rewrite `CollectionConfig` to use it. Final form of the schema section:

```python
class SourceConfig(BaseModel):
    """One root path inside a collection with its own filter chain."""

    path: Path
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    follow_symlinks: bool = False
    frontmatter_filter: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: object) -> object:
        return Path(str(v)).expanduser()

    @field_validator("frontmatter_filter")
    @classmethod
    def _validate_filter(cls, v: str | None) -> str | None:
        # Eagerly compile so a syntax error surfaces at config load with
        # the parser's column. The compiled predicate is rebuilt on demand
        # at index time — caching here would couple the model to runtime.
        if v is None or not v.strip():
            return None
        from acorn.filter_dsl import FilterError, compile_filter

        try:
            compile_filter(v)
        except FilterError as e:
            raise ValueError(f"frontmatter_filter: {e.message} (col {e.column})") from e
        return v


class CollectionConfig(BaseModel):
    """One named set of sources + collection-wide options.

    A collection can be configured in two equivalent shapes:

    * **New (recommended):** ``[[collections.X.sources]]`` — one TOML table
      per source, each with its own includes/excludes/frontmatter_filter.
    * **Legacy:** flat ``roots = [...]``, ``includes = [...]``,
      ``excludes = [...]`` on the collection. Loader normalises this into
      a single implicit source so downstream code only sees the new shape.

    Mixing both forms on the same collection is rejected at load.
    """

    # New shape — primary going forward.
    sources: list[SourceConfig] = Field(default_factory=list)

    # Legacy shape — accepted for backward compat; reconciled below.
    roots: list[Path] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    follow_symlinks: bool = False

    ocr: bool = False  # phase 10 honours this
    ranking_profile: str = "default"

    @field_validator("roots", mode="before")
    @classmethod
    def _expand_roots(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        return [Path(str(p)).expanduser() for p in v]

    @model_validator(mode="after")
    def _normalise_sources(self) -> CollectionConfig:
        if self.sources and self.roots:
            raise ValueError(
                "collection mixes legacy 'roots' with 'sources'; pick one"
            )
        if not self.sources and self.roots:
            # Promote legacy flat shape into a single implicit source.
            implicit = [
                SourceConfig(
                    path=root,
                    includes=list(self.includes),
                    excludes=list(self.excludes),
                    follow_symlinks=self.follow_symlinks,
                )
                for root in self.roots
            ]
            object.__setattr__(self, "sources", implicit)
        return self
```

Add the new import at the top of `acorn/config.py` (alongside `field_validator`):

```python
from pydantic import BaseModel, Field, field_validator, model_validator
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config_sources.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run the rest of the suite — make sure nothing existing broke**

Run: `uv run pytest -q`
Expected: all tests pass; no regressions in `test_collections.py` etc.

If a regression appears: the legacy-shape normaliser usually misses something. Re-read the test that fails and confirm the same fields are populated through the implicit source.

- [ ] **Step 6: Commit**

```bash
git add acorn/config.py tests/test_config_sources.py
git commit -m "feat(config): phase 5.5e-1 — SourceConfig + multi-source CollectionConfig"
```

---

## Task 9: `walk_sources` — per-source filter chain

**Files:**
- Modify: `acorn/walk.py`
- Test: `tests/test_walk_per_source.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_walk_per_source.py`:

```python
"""Phase 5.5e-1: per-source walker."""

from __future__ import annotations

from pathlib import Path

from acorn.config import SourceConfig
from acorn.walk import walk_sources


def _touch(p: Path, body: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_walks_two_sources_with_disjoint_filetypes(tmp_path: Path) -> None:
    md_root = tmp_path / "notes"
    pdf_root = tmp_path / "course"
    _touch(md_root / "a.md")
    _touch(pdf_root / "b.pdf")
    _touch(pdf_root / "ignored.md")  # not in pdf_root's includes
    sources = [
        SourceConfig(path=md_root, includes=["**/*.md"]),
        SourceConfig(path=pdf_root, includes=["**/*.pdf"]),
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["a.md", "b.pdf"]


def test_frontmatter_filter_excludes_non_matching_md(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    _touch(root / "in.md", "---\nCourse: DPwC\n---\nbody\n")
    _touch(root / "out.md", "---\nCourse: Algorithms\n---\nbody\n")
    _touch(root / "no_fm.md", "no frontmatter here\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["in.md"]


def test_frontmatter_filter_only_applies_to_md(tmp_path: Path) -> None:
    """A filter on a source that contains pdf files leaves the pdfs alone —
    the filter is md-only by design (no other format has YAML frontmatter)."""
    root = tmp_path / "mixed"
    _touch(root / "a.md", "---\nCourse: Other\n---\nbody\n")
    _touch(root / "b.pdf", "%PDF-1.4 fake\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md", "**/*.pdf"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    # PDF survives (filter doesn't apply); md fails the filter and is dropped.
    assert paths == ["b.pdf"]


def test_excludes_still_apply_under_filter(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    _touch(root / ".trash" / "trashed.md", "---\nCourse: DPwC\n---\nbody\n")
    _touch(root / "kept.md", "---\nCourse: DPwC\n---\nbody\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md"],
            excludes=["**/.trash/**"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["kept.md"]


def test_invalid_frontmatter_excludes_file(tmp_path: Path) -> None:
    """Per spec: frontmatter parse errors fail closed (filter returns False)
    so a typo in one note doesn't kill the index — but it's also not
    silently included."""
    root = tmp_path / "notes"
    _touch(root / "bad.md", "---\nfoo:\n  nested: not allowed\n---\nbody\n")
    _touch(root / "good.md", "---\nCourse: DPwC\n---\nbody\n")
    sources = [
        SourceConfig(
            path=root,
            includes=["**/*.md"],
            frontmatter_filter="Course == 'DPwC'",
        )
    ]
    paths = sorted(p.name for p in walk_sources(sources=sources))
    assert paths == ["good.md"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_walk_per_source.py -v`
Expected: 5 failures — `walk_sources` doesn't exist.

- [ ] **Step 3: Implement `walk_sources`**

Append to `acorn/walk.py`:

```python
def walk_sources(*, sources: "list[SourceConfig]") -> Iterator[Path]:
    """Yield in-scope paths across every source.

    Per source: applies includes/excludes via :func:`walk`, then on
    ``.md`` files runs the source's frontmatter filter. Frontmatter parse
    errors and missing-field strict-null cases drop the file silently —
    the indexer will eventually log them via ``acorn status --errors``
    (phase 10).
    """
    from acorn.config import SourceConfig  # local import: avoid cycle
    from acorn.filter_dsl import compile_filter
    from acorn.frontmatter import (
        FrontmatterParseError,
        read_frontmatter_from_file,
    )

    for source in sources:
        assert isinstance(source, SourceConfig)
        predicate = (
            compile_filter(source.frontmatter_filter)
            if source.frontmatter_filter
            else None
        )
        for path in walk(
            roots=[source.path],
            includes=source.includes or None,
            excludes=source.excludes or None,
            follow_symlinks=source.follow_symlinks,
        ):
            if predicate is None or path.suffix.lower() != ".md":
                yield path
                continue
            try:
                fm = read_frontmatter_from_file(path) or {}
            except FrontmatterParseError:
                continue
            if predicate(fm):
                yield path
```

Add the missing import at the top of `acorn/walk.py`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from acorn.config import SourceConfig
```

(The `walk_sources` signature uses `"list[SourceConfig]"` as a string forward-reference because `acorn.config` imports from `acorn.walk` for legacy reasons we shouldn't unwind in this task.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_walk_per_source.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full suite to catch regressions**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add acorn/walk.py tests/test_walk_per_source.py
git commit -m "feat(walk): phase 5.5e-1 — walk_sources with per-source filters"
```

---

## Task 10: `build_index_from_config` — per-source extraction

**Files:**
- Modify: `acorn/index.py`
- Test: `tests/test_index_per_source_filter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_index_per_source_filter.py`:

```python
"""Phase 5.5e-1: end-to-end build with one filtered md source + one pdf source."""

from __future__ import annotations

from pathlib import Path

from acorn.config import CollectionConfig, SourceConfig
from acorn.index import build_index_from_config
from acorn.query import Searcher


def _touch(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_only_matching_md_files_indexed(tmp_path: Path, tmp_index_dir: Path) -> None:
    notes = tmp_path / "notes"
    _touch(
        notes / "in_scope.md",
        "---\nCourse: DPwC\n---\n# Note\npenguin sandwich\n",
    )
    _touch(
        notes / "out_of_scope.md",
        "---\nCourse: Algorithms\n---\n# Other\npenguin sandwich\n",
    )
    cc = CollectionConfig(
        sources=[
            SourceConfig(
                path=notes,
                includes=["**/*.md"],
                frontmatter_filter="Course == 'DPwC'",
            )
        ]
    )
    written = build_index_from_config(
        config=cc, collection="coursework", index_dir=tmp_index_dir
    )
    assert written >= 1
    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("penguin sandwich", limit=10, collection="coursework")
    paths = {Path(h.path).name for h in hits}
    assert "in_scope.md" in paths
    assert "out_of_scope.md" not in paths


def test_legacy_flat_shape_still_indexes(tmp_path: Path, tmp_index_dir: Path) -> None:
    root = tmp_path / "papers"
    _touch(root / "a.md", "# A\nblue penguin sandwich\n")
    cc = CollectionConfig(
        roots=[root],
        includes=["**/*.md"],
    )
    written = build_index_from_config(
        config=cc, collection="papers", index_dir=tmp_index_dir
    )
    assert written >= 1
    s = Searcher(index_dir=tmp_index_dir)
    hits = s.search("penguin", limit=5, collection="papers")
    assert any(Path(h.path).name == "a.md" for h in hits)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_index_per_source_filter.py -v`
Expected: failures — `build_index_from_config` still uses the old flat-shape API.

- [ ] **Step 3: Rewire `build_index_from_config`**

Replace `build_index_from_config` in `acorn/index.py`:

```python
def build_index_from_config(
    *,
    config: CollectionConfig,
    collection: str,
    index_dir: Path,
    rebuild: bool = False,
) -> int:
    """Build a collection from its :class:`CollectionConfig`.

    Walks each source's filter chain via :func:`acorn.walk.walk_sources`
    and indexes the surviving paths. The legacy flat-shape config is
    auto-promoted to a single implicit source by the loader, so this
    function only sees the new shape.
    """
    from acorn.walk import walk_sources

    index = _ensure_index(index_dir)
    writer = index.writer(heap_size=_WRITER_HEAP)
    if rebuild:
        writer.delete_documents(F_COLLECTION, collection)
        writer.commit()
    written = 0
    for path in walk_sources(sources=config.sources):
        writer.delete_documents(F_PARENT_ID, _path_parent_id(path))
        for chunk in extract(path):
            writer.add_document(_doc_for_chunk(chunk, collection=collection))
            written += 1
            if written % _COMMIT_BATCH == 0:
                writer.commit()
    writer.commit()
    writer.wait_merging_threads()
    return written
```

(`build_index` itself stays as-is — it's the ad-hoc CLI entry point and doesn't go through `walk_sources`.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_index_per_source_filter.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite check**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add acorn/index.py tests/test_index_per_source_filter.py
git commit -m "feat(index): phase 5.5e-1 — index per-source via walk_sources"
```

---

## Task 11: `acorn collection add` CLI with `--source` / `--filter`

**Files:**
- Modify: `acorn/cli.py`
- Modify: `acorn/config.py` (add `write_collection`)
- Test: `tests/test_cli_collection_add.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_collection_add.py`:

```python
"""Phase 5.5e-1: `acorn collection add` writes [[sources]] via tomlkit."""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

from acorn.cli import app
from acorn.config import load


def _runner_with_config(
    monkeypatch, tmp_path: Path, initial: str = ""
) -> tuple[CliRunner, Path]:
    cfg_path = tmp_path / "config.toml"
    if initial:
        cfg_path.write_text(textwrap.dedent(initial), encoding="utf-8")
    else:
        cfg_path.write_text("", encoding="utf-8")
    # Force the CLI to use the temp config file.
    monkeypatch.setattr("acorn.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return CliRunner(), cfg_path


def test_collection_add_minimal(monkeypatch, tmp_path: Path) -> None:
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app, ["collection", "add", "coursework", "--source", str(notes)]
    )
    assert result.exit_code == 0, result.output
    cfg = load(cfg_path)
    cw = cfg.collection("coursework")
    assert len(cw.sources) == 1
    assert cw.sources[0].path == notes


def test_collection_add_with_filter_and_globs(monkeypatch, tmp_path: Path) -> None:
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app,
        [
            "collection", "add", "coursework",
            "--source", str(notes),
            "--include", "**/*.md",
            "--exclude", "**/.trash/**",
            "--filter", "Course == 'DPwC'",
        ],
    )
    assert result.exit_code == 0, result.output
    s = load(cfg_path).collection("coursework").sources[0]
    assert s.includes == ["**/*.md"]
    assert s.excludes == ["**/.trash/**"]
    assert s.frontmatter_filter == "Course == 'DPwC'"


def test_collection_add_invalid_filter_refuses(
    monkeypatch, tmp_path: Path
) -> None:
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app,
        [
            "collection", "add", "coursework",
            "--source", str(notes),
            "--filter", "Course ==",
        ],
    )
    assert result.exit_code != 0
    assert "col" in result.output.lower()
    # Config file unchanged.
    assert "coursework" not in cfg_path.read_text(encoding="utf-8")


def test_collection_add_appends_to_existing_collection(
    monkeypatch, tmp_path: Path
) -> None:
    """Adding `--source` to an existing collection appends, doesn't replace."""
    initial = """
        [[collections.coursework.sources]]
        path = "/tmp/notes"
        includes = ["**/*.md"]
    """
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path, initial)
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    result = runner.invoke(
        app,
        ["collection", "add", "coursework", "--source", str(pdfs),
         "--include", "**/*.pdf"],
    )
    assert result.exit_code == 0, result.output
    cw = load(cfg_path).collection("coursework")
    assert len(cw.sources) == 2
    assert cw.sources[1].includes == ["**/*.pdf"]


def test_collection_add_preserves_user_comments(
    monkeypatch, tmp_path: Path
) -> None:
    initial = """
        # I love this collection.
        [defaults]
        # global default
        collection = "coursework"
    """
    runner, cfg_path = _runner_with_config(monkeypatch, tmp_path, initial)
    notes = tmp_path / "notes"
    notes.mkdir()
    result = runner.invoke(
        app, ["collection", "add", "coursework", "--source", str(notes)]
    )
    assert result.exit_code == 0, result.output
    text = cfg_path.read_text(encoding="utf-8")
    assert "# I love this collection." in text
    assert "# global default" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_collection_add.py -v`
Expected: 5 failures — `acorn collection add` doesn't accept `--source`/`--filter`/`--include`/`--exclude` (or doesn't exist at all).

- [ ] **Step 3: Add `write_collection` helper to `acorn/config.py`**

Append to `acorn/config.py`:

```python
def write_collection_source(
    *,
    config_path: Path,
    collection_name: str,
    source: SourceConfig,
) -> None:
    """Append ``source`` to ``collection_name`` in the config TOML at
    ``config_path``. Creates the file (and the collection table) if
    needed. Preserves comments and unrelated tables via tomlkit.

    Raises FileNotFoundError if the parent dir is missing — caller is
    expected to mkdir the config dir.
    """
    import tomlkit

    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    collections = doc.setdefault("collections", tomlkit.table())
    collection = collections.setdefault(collection_name, tomlkit.table())
    sources_array = collection.setdefault("sources", tomlkit.aot())  # array-of-tables

    new_table = tomlkit.table()
    new_table["path"] = str(source.path)
    if source.includes:
        new_table["includes"] = list(source.includes)
    if source.excludes:
        new_table["excludes"] = list(source.excludes)
    if source.follow_symlinks:
        new_table["follow_symlinks"] = source.follow_symlinks
    if source.frontmatter_filter:
        new_table["frontmatter_filter"] = source.frontmatter_filter
    sources_array.append(new_table)

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
```

- [ ] **Step 4: Replace the collection-add CLI in `acorn/cli.py`**

Find the `collection_app` definitions in `acorn/cli.py` (around line 146 onwards) and add the new `add` command. Insert after `collection_list` and before `collection_reindex`:

```python
@collection_app.command("add")
def collection_add(
    name: str = typer.Argument(..., help="Collection name."),
    source: list[Path] = typer.Option(
        ...,
        "--source",
        help="Root directory for this collection. Repeat to add multiple.",
    ),
    include: list[str] = typer.Option(
        [],
        "--include",
        help="Glob to include. Repeatable. Defaults to all supported types.",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        help="Glob to exclude. Repeatable.",
    ),
    filter: str | None = typer.Option(
        None,
        "--filter",
        help="Frontmatter filter DSL (md sources only).",
    ),
    follow_symlinks: bool = typer.Option(
        False, "--follow-symlinks/--no-follow-symlinks"
    ),
) -> None:
    """Add (or extend) a collection in the user's config TOML.

    Each invocation appends one source (one --source argument). Repeat
    the command to add additional sources to the same collection.
    """
    from acorn.config import (
        SourceConfig,
        default_config_path,
        write_collection_source,
    )
    from acorn.filter_dsl import FilterError, compile_filter

    if filter is not None:
        try:
            compile_filter(filter)
        except FilterError as e:
            typer.echo(
                f"invalid filter: {e.message} (col {e.column})", err=True
            )
            raise typer.Exit(code=1) from e

    if len(source) != 1:
        typer.echo("--source must be specified exactly once per command", err=True)
        raise typer.Exit(code=1)

    cfg_path = default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    new_source = SourceConfig(
        path=source[0],
        includes=list(include),
        excludes=list(exclude),
        follow_symlinks=follow_symlinks,
        frontmatter_filter=filter,
    )
    write_collection_source(
        config_path=cfg_path, collection_name=name, source=new_source
    )
    typer.echo(f"added source {source[0]} to collection {name} in {cfg_path}")
```

If your typer version warns about `filter` shadowing the builtin: the linter rule is `A002`, which is off in this project. Leave the param name as `filter` — `--filter` is the user-facing flag and that's what matters.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_cli_collection_add.py -v`
Expected: 5 passed.

- [ ] **Step 6: Full suite check**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add acorn/cli.py acorn/config.py tests/test_cli_collection_add.py
git commit -m "feat(cli): phase 5.5e-1 — acorn collection add with --source / --filter"
```

---

## Task 12: `acorn config validate` surfaces filter errors

**Files:**
- Modify: `acorn/cli.py:config_validate`
- Test: `tests/test_config_validate_filter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_config_validate_filter.py`:

```python
"""Phase 5.5e-1: `acorn config validate` reports filter syntax errors."""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

from acorn.cli import app


def _runner(monkeypatch, tmp_path: Path, body: str) -> tuple[CliRunner, Path]:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr("acorn.cli.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("acorn.config.default_config_path", lambda: cfg_path)
    return CliRunner(), cfg_path


def test_validate_passes_for_valid_filter(monkeypatch, tmp_path: Path) -> None:
    runner, _ = _runner(monkeypatch, tmp_path, """
        [[collections.x.sources]]
        path = "~/x"
        frontmatter_filter = "Course == 'DPwC'"
    """)
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output.lower()


def test_validate_reports_filter_syntax_error(monkeypatch, tmp_path: Path) -> None:
    runner, _ = _runner(monkeypatch, tmp_path, """
        [[collections.x.sources]]
        path = "~/x"
        frontmatter_filter = "Course =="
    """)
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "frontmatter_filter" in result.output
    assert "col" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_validate_filter.py -v`
Expected: passes for the valid case probably; fails for the error case if Pydantic's default error rendering is opaque.

- [ ] **Step 3: Improve error rendering in `config_validate`**

In `acorn/cli.py`, replace the existing `config_validate` function:

```python
@config_app.command("validate")
def config_validate() -> None:
    """Validate the config TOML; exit 1 with a helpful message on failure."""
    from pydantic import ValidationError

    from acorn.config import default_config_path, load

    path = default_config_path()
    if not path.exists():
        typer.echo(f"no config at {path}", err=True)
        raise typer.Exit(code=1)
    try:
        cfg = load(path)
    except ValidationError as e:
        # Pydantic packs the column-aware FilterError message into the
        # individual error's ``msg`` field. Surface it line-by-line so the
        # user sees both the location (frontmatter_filter) and column.
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            typer.echo(f"{loc}: {err['msg']}", err=True)
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001 — last-resort surface
        typer.echo(f"invalid config: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(
        f"✓ {path} valid; {len(cfg.collections)} collection(s): "
        f"{', '.join(sorted(cfg.collections)) or '(none)'}"
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config_validate_filter.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite check**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add acorn/cli.py tests/test_config_validate_filter.py
git commit -m "feat(cli): phase 5.5e-1 — config validate surfaces filter syntax errors"
```

---

## Task 13: Phase 5.5e-1 acceptance smoke + plan close-out

**Files:**
- (no source changes — verification only)

- [ ] **Step 1: Run the entire suite**

Run: `uv run pytest -q`
Expected: all tests pass; ~30 new tests in this phase.

- [ ] **Step 2: Lint + types**

Run: `uv run ruff check acorn tests && uv run ruff format --check acorn tests && uv run pyright`
Expected: all clean.

- [ ] **Step 3: Manual smoke against a real Obsidian-style vault (optional but recommended)**

Skip if you don't have a real vault handy; otherwise:

1. Pick a directory with at least one `.md` file containing YAML frontmatter (e.g. `Course: 'X'`).
2. Run:
   ```
   uv run acorn collection add demo --source <vault> --include '**/*.md' --filter "Course == 'X'"
   uv run acorn collection reindex demo
   uv run acorn search "<phrase known to appear in matching note>" --collection demo
   ```
3. Confirm only matching notes appear; toggle to a non-matching value and reindex; confirm the previous matches disappear.

If smoke fails: drop into `acorn config show` to inspect the loaded config; check that `sources` is populated and `frontmatter_filter` round-tripped intact.

- [ ] **Step 4: Update plan §22 (out-of-scope) — drop the `TUI Collection CRUD` deferral once 5.5e-3 lands**

This is a phase 5.5e-3 acceptance gate, not 5.5e-1. **Do not make the change here.** Add it to the 5.5e-3 plan once that's written. (Documenting this here so we don't forget.)

- [ ] **Step 5: Mark task #20 progress**

Update task #20 description to reflect 5.5e-1 done, 5.5e-2 + 5.5e-3 remaining. (Use TaskUpdate; don't push the task to completed yet.)

- [ ] **Step 6: Final close-out commit (if any docs were touched)**

If you didn't touch any docs in step 4, no commit needed. Otherwise:

```bash
git add docs/
git commit -m "docs: phase 5.5e-1 close-out notes"
```

---

## Self-review notes (kept for the executor)

- **Spec coverage check**:
  - SourceConfig data model → Task 8
  - Frontmatter parser → Tasks 2–4
  - Filter DSL → Tasks 5–7
  - Per-source walker → Task 9
  - Index-time filter → Task 10
  - CLI `acorn collection add --source --filter` → Task 11
  - `acorn config validate` filter errors → Task 12
  - `tomlkit` dep → Task 1
  - Acceptance gates → Task 13

- **Type/name consistency**:
  - `compile_filter(text: str) -> Predicate` (raises FilterError) — matches between filter_dsl.py, config.py, walk.py, cli.py
  - `read_frontmatter_from_file(path) -> dict | None` — matches between frontmatter.py and walk.py
  - `walk_sources(*, sources: list[SourceConfig]) -> Iterator[Path]` — matches between walk.py and index.py
  - `write_collection_source(*, config_path, collection_name, source)` — matches between config.py and cli.py
  - `SourceConfig.frontmatter_filter: str | None = None` — matches across all callers
  - `FilterError(message, column)` — matches across emitter (filter_dsl.py) and consumers (config.py, cli.py)

- **Placeholders**: none.

- **Out of scope here (deferred to 5.5e-2 / 5.5e-3 plans)**:
  - `meta_blob` schema field
  - Query-time post-filter via the same DSL
  - Inline `[…]` query-bar syntax
  - TUI Collections form / `F3` binding
  - `acorn collection rm` (currently absent — treat as a separate small task once it's needed; not blocking 5.5e)
