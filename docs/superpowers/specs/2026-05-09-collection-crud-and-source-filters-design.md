# Phase 5.5e — Collection CRUD UI + per-source filters + frontmatter DSL

**Status:** approved (brainstorm 2026-05-09); ready for implementation plan
**Plan refs:** §6 config schema, §8 includes/excludes precedence, §16 phase 5.5e
**Supersedes:** task #20 ("Phase 5.5e: Collection CRUD UI") — same task ID, expanded scope

## Goals

A user must be able to:

1. Add, edit, rename, and remove collections from inside the TUI without opening a text editor.
2. Configure a collection as one or more **sources**, each with its own includes / excludes / metadata filter — so a single collection can pull in `**/*.md` from one tree and `**/*.pdf, **/*.pptx, **/*.docx` from another.
3. Filter the markdown notes pulled from a source by **YAML frontmatter** using a small predicate DSL with comparison, glob, list-membership, and logical operators (e.g. `Course == 'Design Patterns with C++' AND status != 'archived'`).
4. See DSL syntax errors **at save time** in the form (not just at runtime / next config load), and not be allowed to save invalid filters.
5. Save → write back to `config.toml` without destroying user-authored comments / unrelated keys; auto-reindex when the change affects the indexed set.

## Non-goals (explicitly deferred)

- **Regex match** (`=~` operator). Glob (`~~`) covers the most common cases; regex can land later if needed without re-spec'ing.
- **PDF / DOCX metadata filters.** Frontmatter only in this slice; document metadata filtering is a future extension.
- **Concurrent multi-collection editing.** One collection at a time in the form.
- **Live filter preview against real corpus.** A small in-form "test against pasted frontmatter" affordance ships; running the filter against a sampled subset of files is out.

## Architecture

Three units, each independently testable:

```
acorn/
├── frontmatter.py    # parse YAML frontmatter blocks from .md files
├── filter_dsl.py     # parse + evaluate the predicate DSL
├── config.py         # extended: SourceConfig, multi-source CollectionConfig
├── walk.py           # extended: per-source filter chain
├── index.py          # extended: applies frontmatter filter for .md sources
└── tui/
    └── collections_screen.py   # new Textual screen + actions
```

Dependencies between units:

- `frontmatter.py` is a leaf: no acorn imports.
- `filter_dsl.py` is a leaf: no acorn imports.
- `config.py` consumes `filter_dsl.compile_filter` to validate `frontmatter_filter` strings at load time.
- `walk.py` consumes `frontmatter.read_frontmatter` and a compiled filter callable from each source.
- `tui/collections_screen.py` consumes `config.py` (read/write), `filter_dsl.parse_or_error` (validate at save), and `index.py` (kick off reindex).

### New dep

`tomlkit>=0.13` — comment-preserving TOML round-trip. MIT, ~250 KB, same maintainer as poetry. Used only when the form saves the config back; reading still uses stdlib `tomllib`.

YAML frontmatter is hand-rolled (`acorn/frontmatter.py`) to avoid adding PyYAML for one feature. Obsidian's frontmatter is a flat mapping with scalar / list / quoted-string values — well within reach of a ~80 LOC parser.

## Config schema

```toml
[[collections.coursework.sources]]
path     = "~/Obsidian Vault"
includes = ["**/*.md"]
excludes = ["**/.trash/**", "**/templates/**"]
frontmatter_filter = "Course == 'Design Patterns with C++' AND status != 'archived'"

[[collections.coursework.sources]]
path     = "~/Documents/Course/DPwC"
includes = ["**/*.pdf", "**/*.pptx", "**/*.docx"]
# no frontmatter_filter — non-md sources have nothing to filter on

# Legacy shape — single implicit source — still loads:
[collections.papers]
roots    = ["~/Documents/Papers"]
includes = ["**/*.pdf"]
excludes = ["**/Archive/**"]
```

### `SourceConfig` fields

| field | type | notes |
|---|---|---|
| `path` | `Path` | Required. Tilde-expanded at load. |
| `includes` | `list[str]` | Glob whitelist; default empty = all supported types. |
| `excludes` | `list[str]` | Glob blacklist; always wins over includes. |
| `frontmatter_filter` | `str \| None` | DSL expression; only applied to `.md` files. |
| `follow_symlinks` | `bool` | Default `False`. |

### `CollectionConfig` validation

- If both `roots = [...]` and `[[sources]] = [...]` are present → Pydantic ValidationError ("collection X mixes legacy roots= with sources=; use one or the other").
- If `frontmatter_filter` fails to parse → ValidationError with the parser's column/message ("collection X source 1: filter syntax: expected operator after 'Course' at column 8").
- Internally the loader normalizes both shapes into `list[SourceConfig]` so downstream code (walk, index, TUI) only sees the new shape.

## Frontmatter parser (`acorn/frontmatter.py`)

```python
def read_frontmatter(text: str) -> dict[str, object] | None:
    """Parse a YAML frontmatter block at the top of a markdown document.

    Returns None if the document doesn't start with a fenced --- block.
    Returns {} if the block is present but empty. Raises FrontmatterParseError
    on malformed YAML; callers convert that to "filter doesn't match" so a
    typo in one note doesn't kill the index.
    """
```

Supported subset (covers Obsidian, Jekyll, Hugo, MkDocs):

- Block opens with `---\n` on first line, closes with `---\n` or `...\n` on a later line.
- Each line: `key: value` (single key per line — no nested mappings).
- Value forms:
  - Bare scalar (`Course: Design Patterns with C++`)
  - Quoted string (`title: "Final Draft"`, `tags: 'a, b'`)
  - Inline list (`tags: [course, active, dpwc]`)
  - Block list (lines beginning with `  - `)
  - Number (int / float)
  - ISO date (`due: 2026-06-01`)
  - Boolean (`true` / `false`)
  - Null (`~` or `null`)
- Unsupported (raise `FrontmatterParseError`): nested mappings, multiline strings (`|`, `>`), anchors / aliases (`&foo`, `*foo`), tags (`!!str`).

Strings are deserialized as `str`. Numbers as `int` / `float`. Dates as `datetime.date`. Booleans as `bool`. Null as `None`. Lists as `list` of any of the above.

## Filter DSL (`acorn/filter_dsl.py`)

### Grammar

```
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
ident       ::= word | quoted_word               # quoted to allow keys with spaces
```

`OR` / `AND` / `NOT` / `in` / `true` / `false` / `null` are case-insensitive keywords. Comments are not supported (filter is a single expression).

### Operator semantics

| op | meaning | type rules |
|---|---|---|
| `==` | equal | string–string and number–number; string–number → no match |
| `!=` | not equal | as above |
| `<` `>` `<=` `>=` | ordered compare | both operands must be numbers, or both must be ISO dates; otherwise no match |
| `~~` | glob match (string only) | uses `fnmatch.fnmatchcase`; `Course ~~ 'Design *'` |
| `in` / `not in` | list membership | RHS must be a list-valued field; LHS must be a scalar |

### Failure semantics

- A document with no frontmatter at all → filter returns `False` (excluded). Matches "include only notes with `Course = X`".
- A document with frontmatter but missing the field referenced in the filter → `False` for `==`/`<`/etc.; `False` for `in`; `True` for `!=` *only* when the field is present and unequal — a missing field treats the predicate as `False` (strict null-handling).
- A document with frontmatter that fails to parse (`FrontmatterParseError`) → filter returns `False` (excluded), and the indexer logs the file with a `frontmatter_parse_error` flag for `acorn status --errors`.

### Public API

```python
@dataclass(frozen=True)
class FilterError(Exception):
    """Parse-time error with column + message."""
    message: str
    column: int

def parse_or_error(text: str) -> tuple[Predicate | None, FilterError | None]:
    """Used by the TUI form on every keystroke / on save: returns a usable
    predicate or a parse error to show inline. Never raises."""

def compile_filter(text: str) -> Predicate:
    """Used by config loader: returns the predicate or raises FilterError
    (caught by the Pydantic validator and converted to ValidationError)."""

class Predicate(Protocol):
    def __call__(self, frontmatter: Mapping[str, object]) -> bool: ...
```

## Walker (`acorn/walk.py`)

Extended signature:

```python
def walk_sources(*, sources: list[SourceConfig]) -> Iterator[Path]:
    """Yield in-scope paths across all sources for a collection.

    Per-source: applies includes/excludes/follow_symlinks, then frontmatter
    filter on .md files. Frontmatter parsing is lazy — only opened for
    files that survived the cheap path-glob filters.
    """
```

The collection-level `walk(...)` shim continues to work for the legacy single-source shape (collections that haven't migrated). Downstream callers (`build_index_from_config`) switch to `walk_sources`.

## Indexer (`acorn/index.py`)

`build_index_from_config` takes the normalized `list[SourceConfig]` and:

1. For each path yielded by `walk_sources`, call the existing extractor dispatch.
2. For `.md` paths: read frontmatter, evaluate the source's compiled filter, skip if it returns `False` (no extraction, no index entry).
3. Frontmatter parse errors are logged once per file but don't abort the build.

No schema change to the Tantivy index; frontmatter is index-time-only.

## TUI (`acorn/tui/collections_screen.py`)

### New action / binding

| Action | Default key | Command |
|---|---|---|
| `collections.open` | `F3` | `:collections` |
| `collections.save` | `s` (within form) | — |
| `collections.delete` | `d` (within form) | — |
| `collections.reindex` | `r` (within form) | — |
| `collections.cancel` | `Esc` | — |

### Layout

```
Collections                                                              [F3]
─────────────────────────────────────────────────────────────────────────────
   papers        (1 source)               ranking: papers
 > coursework    (2 sources)        *     ranking: default
   notes         (1 source)               ranking: default
   [+ new collection]

─ Editing: coursework ──────────────────────────────────────────────────────
 Name:           [coursework_______________________]
 Ranking:        [default ▾]
 Sources:
   1. ~/Obsidian Vault                            [edit] [remove]
      includes: **/*.md
      excludes: **/.trash/**, **/templates/**
      filter:   Course == 'Design Patterns with C++' AND status != 'archived'
   2. ~/Documents/Course Materials/DPwC           [edit] [remove]
      includes: **/*.pdf, **/*.pptx, **/*.docx
   [+ add source]

─ s save · r reindex · d delete collection · esc cancel ───────────────────
```

### Source-edit panel

```
Edit source — coursework / 1
─────────────────────────────────────────────────────────────────────────────
 Path:     [~/Obsidian Vault                                       ] [browse]
 Includes: [**/*.md                                                  ]
 Excludes: [**/.trash/**, **/templates/**                           ]
 Filter:   [Course == 'Design Patterns with C++' AND status != 'arc⌄]

   ✓ filter parses
   (or)  ✗ filter syntax error at column 12: expected operator

 Test against pasted frontmatter:
 ┌──────────────────────────────────────────────────────────────────┐
 │ ---                                                              │
 │ Course: Design Patterns with C++                                 │
 │ status: active                                                   │
 │ ---                                                              │
 └──────────────────────────────────────────────────────────────────┘
   → matches filter ✓

  s save source · esc cancel
```

### Save flow

1. On `s save` (collection or source): re-parse the filter; if it fails, refuse to save and surface the column + message inline. **The user cannot save an invalid filter.**
2. Successful save → diff the new collection against the on-disk one. If sources / includes / excludes / `frontmatter_filter` changed → kick off `acorn collection reindex <name>` automatically (auto-reindex per user preference, with a "cancel" action visible in the status bar).
3. Write back via `tomlkit`: load the TOML doc, mutate the relevant tables, write back. Comments and unrelated tables are preserved.

### Test-against-frontmatter affordance

The user can paste a frontmatter block (with or without `---` fences) into a small text area; the form runs `read_frontmatter` + the compiled filter and shows ✓ / ✗ live. No file I/O.

### Delete collection

Confirmation modal ("Delete collection 'coursework' and remove its 412 indexed chunks? [y/N]"). On confirm: drop from config, `delete_by_term("collection", name)` on the index.

## Tests

### `tests/test_frontmatter.py` — parser

- Bare scalar / quoted string / inline list / block list / number / date / bool / null
- No frontmatter block → `None`
- Empty block → `{}`
- Unsupported features (nested mapping, anchor) → `FrontmatterParseError`
- BOM / Windows line endings round-trip cleanly

### `tests/test_filter_dsl.py` — parser + eval

- Each operator: `==`, `!=`, `<`, `>`, `<=`, `>=`, `~~`, `in`, `not in`
- AND / OR / NOT precedence and parens
- Quoted identifiers with spaces (`"due date" <= 2026-06-01`)
- Type-mismatch returns `False` (string `<` number)
- Date comparison
- Missing field → `False` (strict null)
- Frontmatter parse error → callable returns `False`
- Hypothesis property: parse-print-parse roundtrip equality

### `tests/test_walk_per_source.py`

- Two sources with disjoint roots, different includes
- Frontmatter filter excludes md files that don't match
- Legacy `roots = [...]` shape still walks
- Frontmatter-filter source ignores non-md files (filter is md-only)

### `tests/test_index_per_source_filter.py`

- Build a collection with one md source + filter, one pdf source no filter
- Only matching md files indexed; all pdf files indexed
- Filter parse error in one note doesn't break the rest of the build

### `tests/test_collections_screen.py` — TUI snapshot + behaviour

- Form opens with collections list (snapshot)
- Empty state: "no collections — press n to add one"
- New collection wizard: name → first source → save
- Edit existing collection: rename + add source → save → reload from disk shows changes
- Save with invalid filter: refused, error shown inline (snapshot)
- Save preserves user comments in `config.toml` (round-trip via `tomlkit`)
- Auto-reindex fires after save when filter changes; cancellable

### `tests/test_config_validate_filter.py`

- `acorn config validate` flags an invalid `frontmatter_filter` with column + message
- Mixing `roots = [...]` and `[[sources]]` → ValidationError

## Phasing — two commits

### Phase 5.5e-1 — Backend (no UI)

1. `acorn/frontmatter.py` + tests
2. `acorn/filter_dsl.py` + tests
3. `acorn/config.py` extended (`SourceConfig`, validators, normalization, tomlkit write API) + tests
4. `acorn/walk.py` extended (`walk_sources`) + tests
5. `acorn/index.py` extended (per-source filter at index time) + tests
6. CLI: `acorn collection add <name> --source <path> [--include ... --exclude ... --filter ...]` (the TUI form will reuse the same `acorn.config` write primitives — no subprocess shelling)
7. `acorn config validate` surfaces filter errors

After 5.5e-1 a power user can configure everything via `acorn config edit` and `acorn collection add`; the form just makes it nicer.

### Phase 5.5e-2 — TUI form

1. `acorn/tui/collections_screen.py` (new screen)
2. New actions in registry; `F3` binding; help-overlay update
3. Source edit modal + filter test affordance
4. Save round-trip via `tomlkit`
5. Auto-reindex hook
6. Snapshot tests + behavioural tests

Each phase ships green with its own commit per §20 phase-gating.

## Acceptance gates

A change is "phase 5.5e complete" only when:

1. All tests above are green; full suite still passes; `ruff` + `pyright --strict` clean.
2. Manual smoke: configure a real Obsidian-vault source with a frontmatter filter via the TUI form, reindex, run a query → only matching notes appear; toggle the filter to its negation, reindex → complementary set appears.
3. `config.toml` saved through the form keeps all hand-authored comments intact (verified by hand on a test file).
4. Invalid filter → form refuses save with inline error; valid filter → saves and (auto-)reindexes.
5. Plan §22 ("Out of scope for v1") is updated to remove the "TUI Collection CRUD" deferral.

## Open questions

None — design approved 2026-05-09. Implementation plan to follow via `superpowers:writing-plans`.
