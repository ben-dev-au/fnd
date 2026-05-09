# Phase 5.5e — Collection CRUD UI + per-source filters + frontmatter DSL

**Status:** approved (brainstorm 2026-05-09); ready for implementation plan
**Plan refs:** §6 config schema, §8 includes/excludes precedence, §16 phase 5.5e
**Supersedes:** task #20 ("Phase 5.5e: Collection CRUD UI") — same task ID, expanded scope

## Goals

A user must be able to:

1. Add, edit, rename, and remove collections from inside the TUI without opening a text editor.
2. Configure a collection as one or more **sources**, each with its own includes / excludes / metadata filter — so a single collection can pull in `**/*.md` from one tree and `**/*.pdf, **/*.pptx, **/*.docx` from another.
3. Filter the markdown notes pulled from a source by **YAML frontmatter** using a small predicate DSL with comparison, glob, list-membership, and logical operators (e.g. `Course == 'Design Patterns with C++' AND status != 'archived'`).
4. **Apply the same DSL at query time** — narrow the result set without making a dedicated collection — by typing the filter inline in the query bar inside square brackets: `[Course == 'DPwC'] strategy pattern`. The bracketed clause becomes a metadata post-filter; the rest is the lexical query.
5. See DSL syntax errors **at save time** in the form (and inline as the user types in the query bar), and not be allowed to save invalid filters.
6. Save → write back to `config.toml` without destroying user-authored comments / unrelated keys; auto-reindex when the change affects the indexed set.

## Non-goals (explicitly deferred)

- **Regex match** (`=~` operator). Glob (`~~`) covers the most common cases; regex can land later if needed without re-spec'ing.
- **PDF / DOCX metadata filters.** Frontmatter only in this slice; document metadata filtering is a future extension.
- **Concurrent multi-collection editing.** One collection at a time in the form.
- **Live filter preview against real corpus.** A small in-form "test against pasted frontmatter" affordance ships; running the filter against a sampled subset of files is out.

## Architecture

Five units, each independently testable:

```
acorn/
├── frontmatter.py    # parse YAML frontmatter blocks from .md files
├── filter_dsl.py     # parse + evaluate the predicate DSL (used at index AND query time)
├── schema.py         # extended: stored meta_blob field for query-time filter
├── config.py         # extended: SourceConfig, multi-source CollectionConfig
├── walk.py           # extended: per-source filter chain
├── index.py          # extended: applies frontmatter filter; serializes meta_blob
├── query_dsl.py      # extended: pre-pass extracts inline [filter] clause
├── query.py          # extended: Searcher post-filters via compiled predicate
└── tui/
    └── collections_screen.py   # new Textual screen + actions
```

Dependencies between units:

- `frontmatter.py` is a leaf: no acorn imports.
- `filter_dsl.py` is a leaf: no acorn imports.
- `config.py` consumes `filter_dsl.compile_filter` to validate `frontmatter_filter` strings at load time.
- `walk.py` consumes `frontmatter.read_frontmatter` and a compiled filter callable from each source.
- `index.py` consumes `frontmatter.read_frontmatter` to serialize the file's metadata into `meta_blob` at index time (so query-time filters can read it back).
- `query_dsl.py` extracts a leading or trailing `[…]` clause from the user query, returns `(lexical_query, metadata_filter_str)`.
- `query.py` consumes `filter_dsl.compile_filter` once, decodes `meta_blob` per hit, applies the predicate as a post-rank step (same shape as `rerank_hits`).
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
3. For surviving `.md` chunks: serialize the file's frontmatter to JSON bytes and store in the `meta_blob` field. Non-md chunks store empty bytes.
4. Frontmatter parse errors are logged once per file but don't abort the build; the offending file is excluded by the index-time filter (already documented above) and `meta_blob` is left empty.

## Schema (`acorn/schema.py`)

Adds one field:

| field | type | indexed | stored | fast | purpose |
|---|---|---|---|---|---|
| `meta_blob` | bytes | no | yes | no | JSON-encoded frontmatter; decoded per hit at query time when a metadata filter is in effect. |

Schema version bumps; existing indexes need a `--rebuild`. The sidecar `.acorn-schema-version` already gates this — old indexes refuse to load with a clear message, matching the established pattern.

## Query DSL pre-pass (`acorn/query_dsl.py`)

The pre-pass already translates `c:` → `collection:` and date / size tokens. It gains one more transform: extract a single bracketed metadata filter from anywhere in the query, leaving the rest as the lexical query.

```python
def split_metadata_filter(query: str) -> tuple[str, str | None]:
    """Return (lexical_query, metadata_filter_or_None).

    Recognises a single top-level [...] block (no nested brackets in v1).
    Bracket appearing inside a quoted phrase is left alone. Whitespace
    surrounding the extracted clause is collapsed.
    """
```

Examples:

| input | lexical | metadata |
|---|---|---|
| `[Course == 'DPwC'] strategy pattern` | `strategy pattern` | `Course == 'DPwC'` |
| `strategy pattern [Course == 'DPwC']` | `strategy pattern` | `Course == 'DPwC'` |
| `strategy pattern` | `strategy pattern` | `None` |
| `"foo [bar]"` | `"foo [bar]"` | `None` (inside phrase) |

Multiple `[…]` blocks → ValueError ("only one inline metadata filter per query"); user can compose with `AND` / `OR` inside the single block.

## Query layer (`acorn/query.py`)

`Searcher.search` and `Searcher.search_grouped` gain an optional `metadata_filter: str | None = None` kwarg. When set:

1. Compile via `filter_dsl.compile_filter` once.
2. Pull `limit * oversample_factor` raw hits from Tantivy (default oversample = 5; raised when filter exclusion is high).
3. For each hit: decode `meta_blob` (empty → `{}`), apply predicate; drop on `False`.
4. Apply rerank, dedup, group as today.
5. Return top-N from the surviving set.

If the post-filter eats too many hits to satisfy `limit`, the next call doubles oversample (capped at `limit * 50`) — bounded so a malicious filter can't make a query expensive.

The TUI calls `Searcher.search_grouped(metadata_filter=metadata_filter)` after splitting via `query_dsl.split_metadata_filter`. A parse error in the bracketed filter surfaces inline under the query bar (same location as Tantivy parser errors) and the search doesn't run.

## Saved searches & history

Saved searches and `Up`/`Down` query history persist the *full* user-typed string including the `[…]` clause — round-trip is "what you typed comes back exactly." No separate column for the metadata filter; the DSL pre-pass extracts it on every replay.

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

### `tests/test_query_metadata_filter.py` — query-time post-filter

- `split_metadata_filter` extracts `[…]` from the start, end, or middle of a query
- A `[…]` block inside a quoted phrase is left alone
- Two `[…]` blocks → ValueError
- Searcher with `metadata_filter="Course == 'DPwC'"` returns only docs whose stored frontmatter matches
- Same DSL, same operators (`==`, `!=`, `<`, `>`, `<=`, `>=`, `~~`, `in`, `not in`, `AND`, `OR`, `NOT`)
- Oversample-and-retry: a strict filter that excludes 90% of raw hits still returns the requested limit (when enough docs match overall)
- Empty `meta_blob` (non-md chunk) under any filter → excluded
- Saved-search round-trip preserves the bracketed clause

## Phasing — three commits

### Phase 5.5e-1 — Backend: index-time filtering

1. `acorn/frontmatter.py` + tests
2. `acorn/filter_dsl.py` + tests
3. `acorn/config.py` extended (`SourceConfig`, validators, normalization, tomlkit write API) + tests
4. `acorn/walk.py` extended (`walk_sources`) + tests
5. `acorn/index.py` extended (per-source filter at index time) + tests
6. CLI: `acorn collection add <name> --source <path> [--include ... --exclude ... --filter ...]` (the TUI form will reuse the same `acorn.config` write primitives — no subprocess shelling)
7. `acorn config validate` surfaces filter errors

After 5.5e-1 a power user can configure filtered collections via `acorn config edit` and `acorn collection add`.

### Phase 5.5e-2 — Query-time filtering

1. `acorn/schema.py` adds `meta_blob` stored field; bump schema version
2. `acorn/index.py` serializes frontmatter JSON to `meta_blob` per chunk
3. `acorn/query_dsl.py` adds `split_metadata_filter`
4. `acorn/query.py` accepts `metadata_filter` kwarg and post-filters with oversample-and-retry
5. CLI: `acorn search [--meta "<filter>"]` for non-TUI users
6. TUI: query bar wires the inline `[…]` syntax; parse errors surface inline; saved searches round-trip the full string
7. Tests: pre-pass split, post-filter end-to-end, oversample correctness, schema-version refusal of old index

After 5.5e-2 the same DSL works at both index time and query time. **Existing indexes must be rebuilt** to gain the `meta_blob` field — old indexes still load (the field is optional in queries that don't use it) only if Tantivy permits adding optional stored fields without rebuild; if not, schema-version gate fires and the user runs `acorn collection reindex --all`. We will determine this empirically during the spike at the start of 5.5e-2 and document the path users must take.

### Phase 5.5e-3 — TUI Collections form

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
2. **5.5e-1 manual smoke** — configure a real Obsidian-vault source with a frontmatter filter via `acorn config edit`, reindex, run a query → only matching notes appear; toggle the filter to its negation, reindex → complementary set appears.
3. **5.5e-2 manual smoke** — without changing the collection's index-time filter, type a query like `[Course == 'DPwC' AND status != 'archived'] design patterns` → results narrow to matching notes within ~100 ms; type a syntactically invalid filter → inline error appears, no search runs; remove the bracketed clause → results widen back.
4. **5.5e-3 manual smoke** — `config.toml` saved through the form keeps all hand-authored comments intact (verified by hand on a test file). Invalid filter → form refuses save with inline error; valid filter → saves and (auto-)reindexes.
5. Plan §22 ("Out of scope for v1") is updated to remove the "TUI Collection CRUD" deferral.

## Open questions

None — design approved 2026-05-09. Implementation plan to follow via `superpowers:writing-plans`.
