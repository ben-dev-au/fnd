# UX pass 2 — bug fixes + polish

**Status:** draft, awaiting approval
**Plan refs:** §5 (UI/UX), §11 (schema)
**Supersedes:** ad-hoc UX work in commits `5c46f61`–`42832d3`

## Context

The first UX overhaul shipped seven panel/styling commits but left twelve concrete issues. This doc covers the second pass: removes wasted real estate, persists scope state, ports markdown rendering to a real renderer, adds source-level scope, and tightens layout density.

Visual references: lazygit's panel layout (narrow left column, dense panels, accent border on the active panel, header-only collapsed sections, full-row cursor highlight, no top chrome).

## Scope summary

| # | Item | Phase |
|---|---|---|
| 1 | Remove top "acorn   scope: DPC" bar | A |
| 2 | Tighter pane padding to match lazygit | A |
| 3 | Reduce left-column width ratio | A |
| 4 | Footer: remove pale-blue full-row tint, keep key-glyph highlight | A |
| 5 | Show `§ heading` / `p.N` / `s.N` location prefix on section rows | A |
| 6 | Left-arrow on already-collapsed root: panel collapses to header-only | B |
| 7 | Persist active scope across sessions | B |
| 8 | Markdown preview: render headings, code blocks, tables, lists, bold/italic | C |
| 9 | Better markdown result context (heading + snippet, not filename repeated) | C |
| 10 | Preview scroll bar: indicate query-match positions | D |
| 11 | Source-level toggle in the Collections panel (schema bump) | E |
| 12 | Score colours — keep as-is ✓ | — |
| 13 | Filters panel — UI for kind / date / size, composed into the query | F |

Phases A–E are sequential; each ends in a commit + screenshot verification.

## Phase A — layout and density (low risk, high visible value)

**A1. Remove the top status bar.** The "acorn   scope: DPC" row is one wasted line. The active scope is already shown in the Collections panel header (`Collections — 1/2 active`). Drop the `#status_bar` widget; the app title is implicit.

**A2. Padding pass.** Lazygit uses `padding: 0 1` on most panels (1 col horizontal, no vertical). We currently use `padding: 1 2` on the preview pane and the default Tree padding inside results / collections. Result: visual gaps at every panel boundary.

  - Preview: `padding: 0 1` (was `1 2`)
  - Tree label padding: `padding: 0 1` already; verify no extra row spacing
  - Results / Collections: `border` only, no internal padding
  - Query bar: `height: 3` is wasteful (full bordered Input). Use `height: 1` plain Input docked top with a `> ` prompt prefix → matches lazygit's command-input style.

**A3. Left column width.** Currently `1fr : 2fr` (33% : 67%). Lazygit ratio is closer to `1fr : 3fr` (25% : 75%). Switch.

**A4. Footer highlight fix.** The pale-blue tint on the full footer row comes from the Static widget being rendered in a dim panel background. The key-glyph reverse style is fine. Fix: drop the panel background on `#footer_hints`, set explicit `background: $surface` to match the screen bg. Keep `[reverse]` markup on the key glyph only.

**A5. Location prefix on section rows.** The user reported "haven't seen any" — investigation will confirm whether `_format_hit_label` is producing `§ heading` correctly and the section rows are being mounted (they should be, but the screenshot only shows file rows because nothing's expanded). Likely fix: ensure result-tree files are auto-expanded for the top N results, and verify the section labels render with their location prefix. Also: when no heading is available on a markdown file, fall back to `:LINE-N` rather than the generic `—` so the prefix is always informative.

## Phase B — persistence + section collapse

**B1. Persistent scope.** Save the active collections list to `~/Library/Application Support/acorn/state/scope.toml`:

```toml
collections = ["DPC", "papers"]
```

Load on `AcornApp.__init__` if no `--collection` arg passed. Save on every toggle in the panel + picker. Tiny TOML file, atomic write (temp + rename) so a crash mid-write can't corrupt it.

**B2. Section collapse-to-header.** When `Left` is pressed and the cursor is on a panel's *root* node (or already at the panel's outermost level), shrink the panel to header-only — its tree disappears and the panel becomes a 3-line strip (border + title + border). Press `Right` while cursor is on the strip (or `Tab` to it then `Right`) to re-expand.

Mechanism: add a `.collapsed` CSS class to the panel widget; when present, set `height: 3` and `display: none` on the inner tree. Toggle the class from `action_tree_smart_collapse` / `_smart_expand` when the cursor is at the relevant boundary.

## Phase C — markdown preview (the big visible win)

**C1. Renderer choice — Rich `Markdown`.** Verified against `glow` in a side-by-side render of a 70-line markdown chunk (`scripts/snap_markdown.py` saves the demo to `/tmp/acorn_md_demo.svg`). All formatting features round-trip correctly: H1–H6 with size differentiation, code blocks with pygments syntax highlighting (`code_theme="monokai"`), Unicode-box tables with column alignment, bullet/numbered lists, blockquotes, bold/italic/strikethrough, horizontal rules, inline code, OSC8 links.

Rich vs. glow stylistic differences:
- Rich centers H1 by default; glow left-aligns. Override via subclassing `Markdown` and replacing the `Heading` element.
- Glow's heading hierarchy uses bordered boxes per level for visual distinction; Rich uses size + colour. Closer to terminal-native, denser. We keep Rich's style — bordered headings inside an already-bordered preview pane would be visually noisy.
- Rich's table padding is slightly tighter than glow's. Acceptable.

Rich is already a transitive dep (Textual ships with it), the renderable mounts cleanly into a `Static`, and the per-chunk scroll architecture is preserved. **Decision: Rich.** If the visual gap grows annoying we revisit with a `glamour` subprocess in a follow-up.

**C2. Per-chunk markdown rendering.** Each preview chunk currently mounts a header `Static` plus N body `Static` widgets (one per logical line, for line-level match highlighting). For markdown chunks:

- Replace the body Statics with a single `Static(Markdown(chunk_text))` per chunk
- Pre-process the markdown to wrap query-term matches in a custom Rich style (`reverse on accent`) so highlighting still works
- Match-target scrolling continues to work because we still know `chunk_seq` → widget mapping

Code blocks, tables, etc. won't have per-line highlight overlays — they render as Rich-styled blocks. The header + chunk-line-match overlay remains for plain text / TXT files.

**C3. Better markdown result context.** Today, when a markdown file scores high it shows up like:

```
DPC Wk8 Notes - Templates...        # the file row
  ► DPC Wk8 Notes...                # section row, just the title repeated
```

That's because for markdown files without explicit `# Heading` lines, the extractor falls back to `title = file basename` and `heading_path = file basename`. So the section row says the same thing as the file row.

**Fix in two steps**:

1. *Extraction*: when a markdown file has no headings, generate synthetic location markers from line numbers — chunk every ~50 lines with `heading_path = "L1-50"`, `L51-100`, etc. The user's markdown notes get useful navigation labels.
2. *Display*: section row format becomes `<location>  "<query-snippet, ~80 chars>"` — the snippet is from the chunk body, not the heading. This matches what users actually want: "where in this file is the match, and what does it say".

The snippet is already in `Hit.snippet` from the searcher — we just weren't surfacing it in the label.

## Phase D — preview scrollbar match indicators

**D1.** Textual scrollbars are a single block; we can't paint them directly. Workaround: add a thin `Static` strip *next to* the preview's scrollbar (right edge, `width: 1`, height matching the scrollable region). Paint cells in the strip at fractional positions corresponding to chunks containing matches. Colour: theme accent. Click on a marker → scroll preview to that chunk.

Alternative considered: overlay coloured dots on the chunk-header rows themselves (cheap, no new widget). Pick whichever reads better visually after a screenshot test.

## Phase E — source-level scope (schema change)

**E1. Schema bump.** `SCHEMA_VERSION = 3`, add a `F_SOURCE_PATH` indexed string field. The indexer writes the source's path at the time of indexing for every chunk. The searcher accepts an `active_sources: list[str] | None` parameter; when non-None, filters via `Query.term_set_query(F_SOURCE_PATH, active_sources)`.

**E2. Migration.** The existing `migrate.prompt_and_rebuild_or_exit` handler already covers this — it'll prompt the user once when they upgrade.

**E3. UI.** Source nodes in the Collections panel get the same `●` / `○` marker treatment as collection nodes. `Enter` on a source toggles it; `Enter` on a collection toggles all its sources. Header counter changes to show `Collections — 1/2 collections, 3/5 sources active`.

**E4. State.** Persistent scope (B1) extends to `active_sources`:

```toml
collections = ["DPC"]
sources = [
    "/Users/me/Documents/Uni/.../Design Patterns with C++",
]
```

## Phase F — Filters panel

**F1. Layout.** A third left-column panel, below Collections, with the same border / collapse / Enter-to-toggle conventions:

```
┌ Filters ─────────────────────────────────┐
│ ▶ File type        (3 of 5 active)        │
│ ▶ Date             (any)                  │
│ ▶ Size             (any)                  │
└──────────────────────────────────────────┘
```

Expanding a category shows its options:

```
│ ▼ File type        (3 of 5 active)        │
│   ● pdf                                    │
│   ● docx                                   │
│   ● pptx                                   │
│   ○ md                                     │
│   ○ txt                                    │
│ ▶ Date             (week)                  │
│ ▶ Size             (any)                   │
```

`Enter` toggles a row. File type is multi-select (toggles independently). Date and Size are single-select radios — toggling a new option turns the previous one off. The panel header tracks an aggregate active count so the user always sees scope state.

**F2. Filter values.**

| Category | Options | Composes to |
|---|---|---|
| File type | pdf · docx · pptx · md · txt | `kind:(pdf docx)` etc. (multiple values join with OR) |
| Date | any · today · week · month · year · `>YYYY-MM-DD` | `mtime:week` etc. — DSL pre-pass already handles these |
| Size | any · `<100KB` · `<1MB` · `<10MB` · `>10MB` | `size:<1MB` |

Free-text path / author / heading filters stay accessible via query syntax (`path:Papers/2024`, `author:Smith`, `heading:Methods`) — putting them in the tree means embedding an Input inside a tree row, which is awkward for the Tree widget. Help overlay (`?`) lists them so they're discoverable. If usage shows the user reaching for those often, we promote them to the panel later.

**F3. Composition.** When the user submits a query (or toggles a filter while a query is active), the search runs against the *composed* query string:

```
typed text:    templates strategy
active filter: kind=pdf+md, date=week
composed:      kind:(pdf md) mtime:week templates strategy
```

The DSL pre-pass we already have translates `kind:` and `mtime:` → field-restricted Tantivy queries. **Zero new query-layer logic.** The Filters panel is purely a UI shell that emits DSL.

**F4. Persistent.** Filter state is included in the Phase B persistence file:

```toml
[scope]
collections = ["DPC"]
sources = ["…"]

[filters]
kind = ["pdf", "md"]
date = "week"
size = "any"
```

Loaded on next launch, applied transparently.

**F5. Why a panel and not chips above the query bar?** Chips would be denser horizontally, but they need either typing-driven autocomplete (more work) or a modal popover for the value list (modal-heavy). The panel is consistent with Collections, supports collapse-to-header for users who don't want it visible, and shows all available filters at once — better discoverability for a tool whose value is search depth.

## Phase ordering rationale

A first → highest visible payoff for least work. B before C → state-persist primitive used by both B (scope) and F (filters). C before D → markdown render must work before scrollbar markers can be tested against it. E forces a reindex (schema bump) — group it with F since both require panel + state work in the same Collections-area flow.

Each phase ends with: failing test → impl → green tests → snapshot screenshot diff → commit.

## Out of scope for this pass

- `glamour` Go-binary integration (Rich Markdown covers the formatting; revisit only if visual gap matters)
- Live preview-scrollbar position synced with cursor (just static markers for now)
- Per-source ranking profiles (still collection-level)
- Preview pane focus / scroll keybinds beyond what we have
- Free-text filter inputs (path / author / heading) in the panel — query syntax remains the path
- Saved filter presets ("workshop notes", "papers only") — easy follow-up once the basic panel ships

## Open questions

None — sign off and I'll write the implementation plan.
