# Settings Menu — UX Redesign (Phase 3)

## Context

The Phase 2 settings menu shipped the structural backbone — a single chrome shared with the main TUI, a 4-category root, drill-able sub-screens, an Esc back-stack, a bottom edit-bar for scalar values, a per-source form with the frontmatter sample tester. Everything navigable. Everything stylable.

Everything *empty*. The structure is right; the user-facing experience falls flat.

A close reading of the user-facing complaints:

1. **The 4-row root reads as bare**. Labels alone don't communicate state, contents, or what each category *does*. A user who opens `:` for the first time sees four words and has to drill into each one to learn anything.
2. **Cross-section search was never delivered**. The plan promised "one search field, filters every leaf across the whole tree." The implementation filters only the current screen. Without cross-section search, the unified command + settings UI the user asked for doesn't exist.
3. **Add Collection got two-stepped**. The old `CollectionsScreen` let the user press `n` then immediately `a` to chain naming with source-adding. The new "Add collection" pops back to the Collections list after naming — the user has to navigate back in to add their first source. *Functionality intact, flow regressed*.
4. **Visual hierarchy is flat**. Colour is used in two tiers ($primary border + $accent focus) and that's it. Nothing draws the eye toward values, keys, or focused content.
5. **No anticipation of confusion**. New users staring at the menu have no idea that `[o]` is a key, that "Preferences" leads somewhere, that `Shift+Enter` does anything different from `Enter`, or that "active collection scope" lives in the sidebar and not in settings.

This spec captures the redesign that addresses all five — driven by the actual user tasks people show up to do, with multiple pathways per task and a deliberate visual language.

## Locked decisions (from the brainstorming round)

1. **Search-first root** — the search input is the most prominent affordance; categories below; descriptions on each row.
2. **Cross-section search** — typing anywhere filters every leaf in the menu, showing flat results with breadcrumb tags. Indexes labels + curated keywords + key glyphs, **not** description prose (which lives in the focused-row detail strip).
3. **Add Collection is a single wizard** — name + source path + multi-select includes + preset-driven excludes + frontmatter filter, on one screen. Ctrl+S saves and reindexes.
4. **Flat search results with breadcrumb-on-the-right**.
5. **Bracketed `[o]` key style in `$accent`** — for the Keybindings screen, used consistently with the hint bar.
6. **Container hugs content** — `height: auto`, centered, max-width ~80–100 chars; expands and scrolls internally only when content demands.
7. **Three colour tiers used with intent** — see Visual System below.
8. **Drill-cue trailing-summary mode is user-configurable** (always-show / smart / always-`…`), default **always-show**.
9. **F3 dropped** — `:` → Collections is the only path.
10. **Reveal-in-Finder pattern** — `Enter` opens, `Shift+Enter` reveals. Hint surfaces in the bottom bar when the focused row supports it.
11. **Press-key-to-invoke** in the Keybindings screen (lazygit's free-training pattern).
12. **Inline errors only** — no toast notifications for in-form failures.
13. **Multi-select for file types** in the Add Source wizard (no free-text glob typing for the common case); preset multi-select for excludes; custom-glob escape hatch in both.

## User research — the use case map

Every reason a user opens the menu, plus the pathways they should be able to take. Pathway columns: **M** = menu navigation; **S** = cross-section search; **K** = direct global shortcut; **I** = press-key-to-invoke (Keybindings only).

### A — Discovery (new / casual user)

| # | Task                                  | M | S | K | I |
|---|---------------------------------------|---|---|---|---|
| A1 | "What can this app do?"              | `:` → scan four categories | — | `?` | — |
| A2 | "What does each key do?"             | `:` → Keybindings           | `?` → type term | `?` | — |
| A3 | "Is there a setting for X?"          | `:` → Preferences → scan    | `:` → type X    | —   | — |
| A4 | Find version / config path           | bottom of root screen       | —               | —   | — |

Implication: the description strip and the bottom status line do real work for new users.

### B — Change a preference

| # | Task                          | M                                                   | S                            | K        |
|---|-------------------------------|-----------------------------------------------------|------------------------------|----------|
| B1 | Result limit                 | `:` → Preferences → row → Enter → value → Enter      | `:` → `result` → Enter       | —        |
| B2 | Debounce                     | as above                                             | `:` → `debounce`             | —        |
| B3 | Toggle highlights            | `:` → Preferences → Highlights → Enter               | `:` → `highlight` → Enter    | `h`      |
| B4 | Default collection           | `:` → Preferences → Default collection → Enter → pick | `:` → `default collection`   | —        |
| B5 | Browse current values        | `:` → Preferences (every row shows its value)        | —                            | —        |
| B6 | Reset a value to default     | (none today) — explicit future feature              | —                            | —        |

Implication: every preference row must render its current value. B6 deferred.

### C — Collections CRUD

| # | Task                                | M                                                    | S                                |
|---|-------------------------------------|------------------------------------------------------|----------------------------------|
| C1 | List collections                   | `:` → Collections                                    | `:` → `collections`              |
| C2 | Create collection + first source   | `:` → Collections → Add → wizard                     | `:` → `add collection` → wizard  |
| C3 | Add another source                 | `:` → Collections → drill → Sources → Add source     | `:` → `<collection name>` → Sources → Add |
| C4 | Edit a source's fields             | drill to source → field row → edit                   | `:` → `<path or filename>` → drills to source |
| C5 | Test frontmatter filter            | open source form → Tab to sample → paste             | — |
| C6 | Rename collection                  | drill → Rename → Enter → type → Enter                | `:` → `rename` → pick collection |
| C7 | Delete collection                  | drill → Delete → confirm                             | `:` → `delete <name>`            |
| C8 | Reindex                            | drill → Reindex                                      | `:` → `reindex` → per-collection rows |
| C9 | Change ranking profile             | drill → Ranking profile → pick                       | `:` → `ranking <collection>`     |

Implication: the cross-section walker must include per-collection rows (so `:` → "default" goes straight to that collection's editor). Per-collection-row trailing values surface source count + active-scope dot.

### D — Runtime scope (sidebar, *not* settings)

| # | Task                              | M (in main app)                       | Pre-empted confusion          |
|---|-----------------------------------|---------------------------------------|-------------------------------|
| D1 | Toggle a collection in scope     | Collections sidebar in main app → Enter | If user searches for "active" / "scope" in settings: return a result that says "Use the Collections sidebar in the main app — press `c`" |
| D2 | Toggle a source in scope         | Collections sidebar → drill → Enter  | Same as above |

Implication: cross-section search includes a "scope" pseudo-row that points the user back to the sidebar.

### E — Keybindings

| # | Task                                | M                                  | S                          | K   | I (press-key-to-invoke) |
|---|-------------------------------------|------------------------------------|----------------------------|-----|--------------------------|
| E1 | List every key                     | `:` → Keybindings                  | —                          | `?` | — |
| E2 | Find a key by description          | scroll                             | `?` → type description     | —   | — |
| E3 | Find what a specific key does      | scroll                             | `?` → type the key (e.g. `o`) | — | — |
| E4 | Invoke an action found in the list | cursor → Enter                     | `?` → search → Enter       | —   | `?` → press the listed key |
| E5 | View only context-relevant keys    | scroll to group                    | `?` → type group name      | —   | — |
| E6 | Rebind a key                       | edit `keybindings.toml`            | `:` → `Open keybindings file` → Enter | — | — |

Implication: Keybindings is both reference *and* launcher. Press-key-to-invoke is the magic moment.

### F — Quick action / palette feel

| # | Task                          | S                            | K   | I |
|---|-------------------------------|------------------------------|-----|---|
| F1 | Run an action by name        | `:` → type → Enter           | —   | `:` → type → Enter |
| F2 | Run explain trace            | `:` → `explain` → Enter      | —   | — |
| F3 | Open multi DSL               | `:` → `multi` → Enter        | —   | — |
| F4 | Quit                         | `:` → `quit` → Enter         | `q` | — |

Implication: F1 *is* the original palette use case. Cross-section search is the only good answer.

### G — Raw config / power user

| # | Task                          | M / S                                      | Extras |
|---|-------------------------------|--------------------------------------------|--------|
| G1 | Open config.toml              | `:` → Open config file → Enter             | `Shift+Enter` reveals in Finder |
| G2 | Open keybindings.toml         | `:` → Open keybindings file → Enter (new)  | `Shift+Enter` reveals |
| G3 | See config path               | trailing column on the "Open config" row, plus root status line | — |

### H — Error paths / unhappy

| # | Task                                  | Pre-empt |
|---|---------------------------------------|----------|
| H1 | Out-of-range scalar                  | inline `must be 1–1000` below edit-bar; field stays open |
| H2 | Save source with non-existent path   | inline `✗ does not exist` next to the path's trailing value; refuse save |
| H3 | Delete the active default collection | confirm dialog mentions "Default collection will fall back to ''" or auto-picks another |
| H4 | Reindex with zero sources            | refuse with inline "No sources to index" on the Reindex row |
| H5 | `$EDITOR` unset                      | fallback to `vi`, status notify (already done) |
| H6 | Frontmatter sample doesn't parse     | inline `✗ frontmatter parse error: <msg>` (already done) |
| H7 | Invalid TOML after editor edit       | push `ConfigRecoveryScreen` (already done) |

## Design system

### Visual hierarchy — three colour tiers, used with intent

| Tier        | Token        | What it's for                                                                                |
|-------------|--------------|----------------------------------------------------------------------------------------------|
| Active      | `$accent`    | focused container border, cursor row background (`$accent 40%`), group sub-headers (bold), key glyphs in Keybindings, the `▌` cursor bar, the static `/` glyph next to the search input |
| Emphasis    | `$primary`   | setting *values* in trailing columns — bold so the eye lands on `200`, `On`, `default` first when scanning a list |
| Default     | `$text`      | labels, body content, descriptions in the detail strip                                       |
| Structural  | `$text-muted`| dotted leaders (`·······`), breadcrumb tags, hint bar text, drill-row trailing summaries, the description strip prefix |
| Semantic    | `$success`   | `✓ filter parses`, `✓ sample matches filter`, `✓ 1,247 files`                                |
|             | `$warning`   | `⚠ path not found`, `⚠ requires reindex`                                                     |
|             | `$error`     | inline validation errors, `✗ does not exist`, `✗ filter syntax: col 5`                       |

The user's existing theme (Tokyo-night) supplies the literal colours; everything is a theme token so future theme switches Just Work.

### Container

Every settings screen uses the same container:

- `Vertical#settings_box`, `border: round $primary 50%` muted, `:focus-within { border: round $accent }` bright when focused.
- `border_title` set to the breadcrumb (`Settings & Commands`, `Settings & Commands › Preferences`, …).
- `height: auto` (hugs content), `max-height: 90%` of screen, `width: auto` capped at `100`.
- `align: center middle` on the parent Screen so the box centres horizontally; vertically anchored near the top for short content, expanding downward as needed.
- Bottom of the box: a 2-line **detail strip** showing the focused row's description + ancillary info, separated from the list by a thin dim `─` rule.
- Below the box (docked to screen): the shared anchor + contextual hint bar.

### Row anatomy

Every row in every settings screen renders this layout (Rich Text, no widget per cell):

```
▌ KEY    LABEL  ··········  TRAILING
```

- `▌` cursor bar: `$accent`, rendered only on the cursor row.
- `KEY` column: present only in Keybindings rows. Width 8 chars including brackets. Rendered as `[<key>]` in `$accent`.
- `LABEL` column: `$text`.
- Dotted leader (`·`) and `TRAILING`:
  - Trailing **value** (for `KIND_SCALAR` / `KIND_TOGGLE` / `KIND_PICKER`): bold `$primary`, right-aligned.
  - Trailing **summary** (for drill rows, when "always show" mode is active): `$text-muted`, right-aligned, e.g. `Result limit · Debounce · Defaults · 4`.
  - Trailing **breadcrumb** (when a row is rendered as a cross-section search result): italic `$text-muted`, right-aligned, e.g. `Preferences › Search`.
- Sub-headers (group titles like `Global`, `Search behaviour`): bold `$text` (one level only), with a half-line of top padding for grouping. No rule lines, no extra colour.

### Key style — bracketed accent

In the Keybindings screen and the hint bar, keys render as `[<key>]`:

```
    [/]      Focus the search bar
    [:]      Open settings & commands
    [Space]  Quick Look
    [Ctrl+C] Quit
```

- Brackets in `$text-muted` (subtle frame).
- Key glyph in `$accent`, bold.
- Multi-character keys (`Space`, `Ctrl+C`, `Tab`) render the same way, no special-casing.

### Detail strip

A 2-line area at the bottom of each container, inside the border, separated by a thin dim `─` rule. Shows:

- Line 1: the focused row's `description` field, plain `$text`.
- Line 2: ancillary metadata in `$text-muted`, prefixed with the data type and any constraints:
  - For settings: `Stored in defaults.result_limit (1–1000) · Applies on next search`
  - For drill rows: `<n> items` or a content summary
  - For actions: `Runs <action_id>` and the binding it's currently mapped to
  - For drill into a sub-section: nothing (the trailing summary in the row already serves this)

The detail strip is dynamic: empty when no row is focused, populated on highlight. Keeps the menu informative without bloating the row.

### Hint bar (shared with main app)

The bottom anchor+contextual hint bar built by `render_hint_bar(anchors, contextual)` (already extracted). The Settings menu's contextual cluster:

- Default: `↑↓ Nav · ⏎ Open · ← Back · / Filter`
- With a focused reveal-capable row: append `· Shift+⏎ Reveal`
- Edit-bar open: `⏎ Save · Esc Cancel`
- Search field focused: `↓ Results · ⏎ Open first · Esc Clear`
- Keybindings screen: `⏎ Run · [key] Run directly · Esc Back`

## Information architecture

### Root (`:` → opens this)

Four rows. No headers. Search input on top. Detail strip on the bottom.

```
   ┌─ Settings & Commands ────────────────────────────────────────────┐
   │  /  Type to filter…                                              │
   │                                                                  │
   │ ▌ Preferences         Result limit · Debounce · Defaults · 4     │
   │   Collections         2 collections · 5 sources                  │
   │   Keybindings         24 keys across 6 contexts                  │
   │   Open config file    …/Application Support/fnd/config.toml    │
   │                                                                  │
   │ ──────────────────────────────────────────────────────────────── │
   │  Preferences — adjust result limit, debounce, default collection,│
   │  highlights, and ranking profile.                                │
   └──────────────────────────────────────────────────────────────────┘
    / Filter   ⏎ Open   Esc Back                       Shift+⏎ Reveal
```

Trailing summaries:
- **Preferences**: count of leaf settings + brief contents preview
- **Collections**: `<N> collections · <M> sources` (live state)
- **Keybindings**: `<N> keys across <M> contexts` (live count)
- **Open config file**: the config path, truncated from the left

When the user types into the search input, the list reflows to cross-section matches (see Search section).

### Preferences sub-screen

```
   ┌─ Settings & Commands › Preferences ──────────────────────────────┐
   │  /  Type to filter…                                              │
   │                                                                  │
   │   Search behaviour                                               │
   │ ▌   Result limit                                          200    │
   │     Debounce (ms)                                         200    │
   │     Preview chunks                                          5    │
   │                                                                  │
   │   Display                                                        │
   │     Highlights                                             On    │
   │                                                                  │
   │   Defaults                                                       │
   │     Default collection                                default    │
   │                                                                  │
   │ ──────────────────────────────────────────────────────────────── │
   │  Result limit (1–1000) — max results returned per query.         │
   └──────────────────────────────────────────────────────────────────┘
    ⏎ Edit   Esc Back
```

- Three sub-groups (sub-headers, bold `$text` with top padding): Search behaviour · Display · Defaults.
- Right-aligned values in `$primary` bold (`200`, `On`, `default`).
- Cursor row gets `▌` and accent-tinted background.

### Collections sub-screen

```
   ┌─ Settings & Commands › Collections ──────────────────────────────┐
   │  /  Type to filter…                                              │
   │                                                                  │
   │ ▌ Add collection                                                 │
   │   default                              ● 3 sources · ranking:default │
   │   research                             ○ 1 source · ranking:academic │
   │                                                                  │
   │ ──────────────────────────────────────────────────────────────── │
   │  default — 3 sources. Active in the current search scope.        │
   │  ●/○ shows scope state. Press Enter to manage; Tab to toggle in  │
   │  the main app sidebar (`c`).                                     │
   └──────────────────────────────────────────────────────────────────┘
    ⏎ Open   Esc Back
```

- Each collection row's trailing shows source count + ranking profile shorthand. The `●` / `○` indicates current scope state.
- Cursor on `Add collection` shows the wizard description in the detail strip.

### Add Collection wizard

```
   ┌─ Add Collection ─────────────────────────────────────────────────┐
   │                                                                  │
   │ ▌ Name                                              research_    │
   │   Source path                              ~/Documents/Notes ✓   │
   │   Includes                                   3 file types  ▶     │
   │   Excludes                                   4 presets · custom: │
   │                                                drafts/**         │
   │   Frontmatter filter (optional)                  type:note  ✓    │
   │   Follow symlinks                                       Off      │
   │                                                                  │
   │ ──────────────────────────────────────────────────────────────── │
   │  Test the filter against a sample frontmatter:                   │
   │   ┌────────────────────────────────────────────────────────────┐ │
   │   │                                                            │ │
   │   └────────────────────────────────────────────────────────────┘ │
   │  (no sample)                                                     │
   └──────────────────────────────────────────────────────────────────┘
    ⏎ Edit   Tab Sample   Ctrl+S Save & Index   Esc Cancel
```

Field-by-field behaviour:

- **Name**: required. Inline edit bar (text). Validates uniqueness on save; if name already exists, inline error.
- **Source path**: required. Inline edit bar. Strips wrapping quotes. Expands `~`. On *every keystroke*, validates: if path exists, trailing column shows `✓ <N> files` (capped count, fast `os.scandir` walk); if not, shows `✗ does not exist` in `$error`.
- **Includes**: opens a **multi-select picker** with the indexer-supported file types pre-listed:
  - Markdown (.md)
  - PDF (.pdf)
  - Word (.docx)
  - PowerPoint (.pptx)
  - Plain text (.txt)
  - Custom glob… (text input)

  Trailing column on the wizard row shows `<N> file types` (or `<N> + custom` when custom globs are present).

- **Excludes**: opens a **multi-select picker** with curated preset patterns, **pre-checked for safe defaults**:
  - ☑ Hidden / system (`**/.*`, `**/.DS_Store`, `**/.git/**`)
  - ☐ Node modules (`**/node_modules/**`)
  - ☐ Python caches (`**/__pycache__/**`, `**/*.pyc`)
  - ☐ Build artefacts (`**/dist/**`, `**/build/**`)
  - ☐ Obsidian metadata (`**/.obsidian/**`)
  - Custom globs… (free text)

  Trailing column: `<N> presets` (e.g. `4 presets · custom: drafts/**`).

- **Frontmatter filter**: optional. Inline DSL text input. Live parse-status in the trailing column (`✓` / `✗ col 5`).
- **Follow symlinks**: toggle, On/Off.

The frontmatter sample tester docks below the field list (Tab cycles from the field list into it and back), same logic as today — using the live `filter` field's value.

On `Ctrl+S`: validate everything; refuse with inline errors if any field is bad; otherwise write the new collection via `write_collection`, kick off `_reindex_collection_async(name)`, pop the wizard, drop the user on the per-collection sub-screen for the new collection (so they can immediately add more sources or jump to a setting).

`Esc` cancels — even if the user typed a name, *nothing is written*. (Distinct from old behaviour where naming created an empty collection. Reduces the "I named it and now there's an empty thing" surprise.)

### Per-collection sub-screen

```
   ┌─ Settings & Commands › Collections › default ────────────────────┐
   │  /  Type to filter…                                              │
   │                                                                  │
   │ ▌ Rename                                                         │
   │   Sources                                          3 sources     │
   │   Ranking profile                                    default     │
   │   Reindex                                                        │
   │   Delete collection                                              │
   │                                                                  │
   │ ──────────────────────────────────────────────────────────────── │
   │  Rename — change the collection name. Chunks reindex under the   │
   │  new name.                                                       │
   └──────────────────────────────────────────────────────────────────┘
    ⏎ Open   Esc Back
```

Five rows: Rename, Sources, Ranking profile, Reindex, Delete collection. Each gives a clear description on focus. Reindex shows a notification on click; Delete pushes a confirm sub-screen.

### Sources sub-screen

```
   ┌─ Settings & Commands › Collections › default › Sources ──────────┐
   │  /  Type to filter…                                              │
   │                                                                  │
   │ ▌ Add source                                                     │
   │   1. ~/Documents/Notes               3 file types · 4 excludes   │
   │   2. ~/Documents/Papers              PDF only · ⚠ path not found │
   │   3. ~/Documents/Slides              PowerPoint only             │
   │                                                                  │
   │ ──────────────────────────────────────────────────────────────── │
   │  ~/Documents/Notes (source 1) — Markdown, PDF, Word.             │
   │  Excludes: hidden / git / Obsidian metadata.                     │
   └──────────────────────────────────────────────────────────────────┘
    ⏎ Open   Esc Back
```

- Each source row's trailing summary shows the file-types selected (or "Custom" if user is using custom globs), plus path validation state.
- `⚠ path not found` warning surfaces on the trailing column if the path no longer resolves; the row still drills (so the user can fix it).

### Per-source form

Same shape as the Add Collection wizard, but pre-populated with the existing source's values. Save updates the collection's source list and triggers reindex if the source actually changed.

### Keybindings sub-screen (`?` opens directly)

```
   ┌─ Settings & Commands › Keybindings ──────────────────────────────┐
   │  /  Type to filter…                                              │
   │                                                                  │
   │   Global                                                         │
   │     [/]      Focus the search bar                                │
   │ ▌   [:]      Open settings & commands                            │
   │     [?]      Open keybindings                                    │
   │     [q]      Quit                                                │
   │     [Ctrl+C] Quit (force)                                        │
   │                                                                  │
   │   Results pane                                                   │
   │     [o]      Open at locator                                     │
   │     [O]      Open in default app                                 │
   │     [Space]  Quick Look                                          │
   │     [h]      Toggle search highlights in the preview             │
   │     ...                                                          │
   │                                                                  │
   │ ──────────────────────────────────────────────────────────────── │
   │  Open settings & commands — opens this menu. Stored in           │
   │  `fnd.tui.actions:open_command_palette`.                       │
   └──────────────────────────────────────────────────────────────────┘
    ⏎ Run   [key] Run directly   Esc Back
```

Behaviour highlights:
- Sub-headers (Global · Results pane · Preview pane · Filters panel · Collections panel · Settings menu) bold, no rule lines.
- Press-key-to-invoke: while this screen is up, pressing one of the listed keys (e.g. `o`) dispatches that action and closes the menu — the canonical "free training" affordance.
- `?` from anywhere in the main app pushes this screen directly. Single Esc returns.
- Cross-section search includes every key row, indexed on `<key>` and `<description>`.

### Open config file / Open keybindings file (root-level actions)

Two sibling rows on the root menu:
- `Open config file` — `~/Library/Application Support/fnd/config.toml`
- `Open keybindings file` — `~/Library/Application Support/fnd/keybindings.toml`

Each:
- `Enter` → drops to `$EDITOR` via `App.suspend()`, reloads + validates on return (recovery flow if invalid).
- `Shift+Enter` → reveals in Finder (`open -R <path>` on macOS).
- Trailing column shows the path truncated from the left.
- Hint bar appends `Shift+⏎ Reveal` when one of these rows is focused.

## Search behaviour

### Index

Built lazily on each settings screen open. For every leaf in the tree:

- **Indexed**: `label`, every entry in `keywords`, the `key` glyph (if set), the path segments of the breadcrumb (`Preferences`, `Search behaviour`).
- **NOT indexed**: full `description` prose (avoids result muddying — descriptions surface in the detail strip only).

Match algorithm: case-insensitive substring on the concatenation. Score = match-position-from-start of the label (earlier match → higher rank), with ties broken by length (shorter label first). Simple and predictable; can be upgraded to fuzzy later.

### Activation

The search box lives on every settings screen. When the search input is non-empty, the local row list is replaced by the flat cross-section matches.

Activating a result dispatches by kind:

- **scalar / toggle / picker**: open the appropriate editor on the *current screen* — no need to navigate to its home sub-screen first. The user gets the result inline.
- **action (KIND_ACTION with `action_id`)**: close the settings stack, dispatch the action.
- **external (KIND_EXTERNAL)**: invoke its callable directly. Most externals push their own screens (per-collection editor, source form, etc.) — those just stack on top.
- **header rows are never in the index** (skipped at walk time).

### What's included in the cross-section walk

For every section provider:
- Preferences: every scalar / toggle / picker row (Result limit, Debounce, Highlights, Default collection, …)
- Collections: `Add collection`, each per-collection drill row (`default`, `research`, …)
- Keybindings: every key row from every group
- Root-level actions: `Open config file in editor`, `Open keybindings file in editor`
- Pseudo-rows for known confusions: `Active collection scope → use sidebar (c)` (so users searching "scope" get pointed back to the right place)

### Match display

```
   ▌ Result limit           200          Preferences › Search behaviour
     Preview chunks         5            Preferences › Search behaviour
     Reindex default                     Collections › default
```

- Label on left.
- Trailing value in `$primary` bold (for settings), then breadcrumb on the right in italic `$text-muted`.
- Bold-substring of the matched query inside the label (e.g. typing `result` bolds the `result` substring in "Result limit").

### Empty-state hint

When the search input is non-empty and there are zero matches: replace the row list with `Static("No matches for '<query>'. Try shorter terms or press Esc to clear.")` in `$text-muted`.

## Navigation rules (unchanged from Phase 2, restated)

| In...                                  | ↑/↓ (j/k) | →/⏎          | ←          | Esc                                  |
|----------------------------------------|------------|--------------|------------|--------------------------------------|
| Main app                               | per pane   | per pane     | per pane   | cascade: pane → results              |
| Settings root                          | nav        | → drill only; ⏎ activate | pop (close menu) | clear search if any, else pop |
| Settings sub-screen                    | nav        | → drill only; ⏎ activate | pop one level | clear search if any, else pop |
| Edit bar focused                       | n/a        | ⏎ save (Right inert) | n/a    | cancel, restore focus to row    |
| Search input focused                   | ↓ to list  | first result | un-focus   | clear, un-focus                 |
| Picker (single-select)                 | nav        | select + pop | n/a        | cancel + pop                    |
| Picker (multi-select)                  | nav        | toggle ✓     | n/a        | commit + pop                    |

`Shift+Enter` triggers the reveal-in-Finder branch on supported rows (Open config file, Open keybindings file, per-source rows where the path exists).

Right-arrow is **navigation parity only** — drills sub-screens, but on scalars / toggles / actions it's a deliberate no-op. Enter is the activate key.

## Drill-cue user preference

A new Preferences row, **Display › Drill row summaries**, controls how drill rows look. Three modes:

- **Always show** (default): every drill row carries a content summary in the trailing column.
- **Smart**: only rows with real content (counts, settings names) show summaries; thin rows like "Rename" are bare.
- **Always `…`**: a uniform dim `…` ellipsis on every drill row. No content summary.

Stored as `defaults.drill_summary_mode` in `config.toml`. Backed by a picker.

### Cross-section search is global

Search is cross-section *from every settings screen*, not just the root. Typing on the Keybindings screen, the Preferences screen, or any sub-screen filters every leaf in the menu. The user's mental model is "I want to find a thing", not "I'm currently navigating Preferences, so search Preferences" — the latter would create dead-ends.

Activation rule for matches that don't live on the current screen:

- **scalar / toggle**: the edit bar opens on the current screen with the matched item's label and value. After save, the original screen's row list (if visible after Esc) shows the updated value. The screen the user is on doesn't change — they searched, they edited, they keep going.
- **picker**: the picker pushes on top of the current screen as it would anywhere else.
- **action / external**: dispatch as usual (action closes the menu and runs; external pushes its target screen on top).

The user's last-visited screen is preserved unless an action takes them somewhere new. No surprise navigation.

## Out of scope (deferred to a later phase)

- Reset-to-default action per setting row (B6).
- Fuzzy matching for cross-section search (current spec is substring).
- Rebind-a-key UI (E6); the user edits `keybindings.toml` for now.
- Inline file browser for the Source path field — text input with validation suffices.
- "Recent / pinned settings" surfacing.

## Verification (manual + tests)

Manual walkthrough after implementation:

1. **Root reads as informative**: `:` opens a compact box (~10 rows tall, centered). Each category row shows what's inside in dim trailing text. The focused row's description appears in the detail strip below.
2. **Cross-section search works**: type `result` on the root → flat results across Preferences (Result limit, Preview chunks) and any matching collection / action.
3. **Activating a flat result from root**: Enter on a search match for "Result limit" → edit bar opens *on the root screen* with the value `200` populated. Submit updates the value. Detail strip updates.
4. **Add Collection is one screen**: `:` → Collections → Add collection → fill name, path, tick three file types in the Includes picker → Ctrl+S → collection saved, reindex starts, user lands on the new collection's per-collection sub-screen.
5. **Esc cancels the wizard with no side effects**: same flow, but Esc after typing a name → nothing saved.
6. **Path validation is live**: type `/nonexistent/path` in the Source path edit bar — trailing column flips to `✗ does not exist` in red. Switch to a valid path — flips to `✓ N files`.
7. **`?` opens Keybindings directly**: cursor lands in Global group. Esc returns in one press.
8. **Press-key-to-invoke**: open `?`, press `o` (while focus is on the screen, not the search input). The settings stack closes and the focused result opens.
9. **Reveal-in-Finder**: `:` → Open config file → cursor on row → footer bar shows `Shift+⏎ Reveal`. Press `Shift+Enter` → Finder opens with the file selected.
10. **Drill-cue mode toggles**: Preferences → Display → Drill row summaries → pick "Always …" → back out → all drill rows now show a dim `…`.
11. **Keys render bracketed**: Keybindings screen shows `[o]`, `[Space]`, `[Ctrl+C]` etc. in `$accent`.
12. **Detail strip updates on focus**: every cursor move updates the description below the list.

Automated tests:

- Cross-section walker returns expected items (every preference, every collection, every keybinding row + the open-config-file / open-keybindings-file actions).
- Activating a scalar search match opens the edit bar on the current screen with the right initial value.
- Path validation produces inline `✓` / `✗` states in the wizard.
- Pressing `o` while the Keybindings screen is up dispatches `action_open_at_locator` and pops the screen.
- `Shift+Enter` on the Open config file row triggers `open -R <path>` (mocked subprocess in tests).
- Drill-cue mode setting round-trips through `config.toml`.

## File-by-file change map

| File                                      | Change                                                                                  |
|-------------------------------------------|-----------------------------------------------------------------------------------------|
| `fnd/tui/menu.py`                       | Add `walk_all_sections()` walker. New `KIND_HEADER` rendering changes (none, already supports). New top-level `Open keybindings file` row. Add scope pseudo-row. Drill-cue mode helper. Trailing-summary providers for every drill row (live counts). |
| `fnd/tui/settings_screen.py`            | `SettingsScreen`: container `height: auto`, centered, max width 100. Detail strip widget below list. Cross-section search on root. Match-substring bolding in row renderer. Bracketed key style. Reveal-in-Finder binding for supported rows. New Add Collection wizard screen + multi-select picker integration. Per-source form retains TextArea tester; switches to picker-based Includes / Excludes. |
| `fnd/tui/app.py`                        | Drop F3 binding. Hint-bar table gets reveal-aware variant. Open config / keybindings file actions. |
| `fnd/config.py`                         | New `defaults.drill_summary_mode` field. Indexer-supported file types as a public constant `INDEXER_FILETYPES`. |
| `fnd/tui/widgets/`                      | (new dir if needed) — maybe split EditBar / DetailStrip / row-renderer helpers into their own files since `settings_screen.py` is large. |
| `tests/test_settings_redesign.py`         | New file covering all twelve verification steps above. |
| `tests/test_actions_keymap.py`            | Drop the stale `:` behaviour test, replace with cross-section search dispatch test. |

## Implementation plan

To be written in `docs/plans/2026-05-11-settings-menu-redesign.md` after this spec is approved.
