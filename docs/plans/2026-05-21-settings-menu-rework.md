# Settings Menu Rework — Audit & Plan

**Branch:** `feat/real-pdf-support`
**Date:** 2026-05-21
**Trigger:** Phase 1-2.5 user-facing surfaces (extras, cache, indexer) were
bolted onto the settings menu without IA framework or chrome discipline.
The first user pass found broken navigation, inconsistent visuals, and
filler menu items. This doc audits what shipped, what tests missed, and
the rework — under framework **B (subject-domain, with an Indexing hub)**.

---

## Part 1 — Audit of the uncommitted settings work

The work lives in the uncommitted diff on top of `1f42d2f`. Everything
below is in `fnd/tui/menu.py`, `fnd/tui/settings_screen.py`,
`fnd/tui/actions.py`, `fnd/config.py`, `fnd/tui/app.py`,
`tests/test_settings_extras_indexing_cache.py`.

### 1.1 What works (keep, with tweaks)

| Piece | Status | Notes |
|---|---|---|
| `defaults.indexer_auto_resume` field on `Defaults` | OK | Pydantic-declared, round-trips through `write_setting`. Keep. |
| `app.py` reads `cfg.defaults.indexer_auto_resume` directly | OK | Replaces the `getattr` fallback. Keep. |
| `MenuItem(toggle_getter/setter)` for auto-resume | OK | Will be reused inside the new Indexing screen. |

### 1.2 What's broken (rework)

**A. Bespoke screens that ignore the settings-chrome contract.**

| Screen | Class | What's wrong |
|---|---|---|
| `ExtraDetailScreen` | `Screen[None]` | Custom CSS, no `#settings_box`, no `DetailStrip`, no consistent hint bar, runs `uv sync` inside the running venv, streams subprocess stdout into a `Static`, `Esc` blocks with a notification when "running," no cancel path. Enter triggers irreversible work without a Yes/Cancel selection step. |
| `CacheInfoScreen` | `Screen[None]` | Standalone Input + Static result. No SettingsList, no DetailStrip. Path-typed input has no validation. Low product value — same as `fnd cache info <path>` with no realistic in-TUI use case. |
| `CacheMaintenanceConfirm` | `Screen[None]` | Closest to the right pattern (uses `OptionList`) but has its own CSS (`border: round $warning`) instead of inheriting from `DeleteCollectionScreen`'s pattern. Diverges visually without benefit. |

**B. IA placement is incoherent.**

Three root-level rows added — Extras, Indexing, Cache — at the same
level as Preferences. Rationale was "they each have their own actions"
but Preferences already houses many actions under one roof. The
asymmetry leaves the user without a mental model for where new settings
will appear in future.

**C. Filler / duplicative entries.**

| Row | Problem |
|---|---|
| `Indexing → "Reindex default collection"` | Duplicates Collections' per-collection reindex. Action `reindex_default` was added to REGISTRY for this. Both go away. |
| `Indexing → "Show first-reindex warning again"` | Maintenance trapdoor for one-shot UI. If reset is ever needed in practice, the warning's design failed. Replace with stronger in-context disclosure on the extras install screen itself. |
| `Cache → "Inspect file in cache…"` | CLI-only debug tool with no plausible in-app workflow. Remove from TUI; keep CLI command. |

**D. Async / loading-state mishandling.**

`_provider_extras` calls `actual_disk_mb()` (walks the filesystem) from
inside `value_getter`, which is called synchronously on every render.
First paint shows blank rows until the walk finishes. No placeholder,
no spinner, no caching.

**E. Detail-strip and metadata absent.**

Existing settings rows set `setting_path`, `hint`, `description`, and
`action_id` so `SettingsScreen._row_metadata` populates the
`DetailStrip` on cursor change. Most new rows have empty descriptions
or hints, so the bottom strip stays blank — the user loses the
second-line affordance the rest of the menu provides.

**F. External-app rows lack a divider and a glyph.**

`Open config file` / `Open keybindings file` sit immediately after
keybindings with no separator. They open `$EDITOR` — a context switch
the user can't undo with `Esc`. Should be visually demarcated.

**G. Per-collection reindex routing change has no test.**

`_make_reindex` now routes through `_reindex_with_warning_if_needed`
(was `_reindex_collection_async`). The change is desirable but lands
silently — no test asserts the modal opens, no test asserts the
collection-name argument flows through `count_pdfs` correctly.

### 1.3 Specific bugs the user hit

1. **"Empty rows on first load."** ☑ Issue 1.2.D.
2. **"Arrows trigger toasts on the extras detail screen."** Bindings on
   `ExtraDetailScreen` only handle `enter`/`escape`/`q`. Arrow keys
   have no handler so they bubble up to whatever parent screen happens
   to be on the stack. ☑ Issue 1.2.A.
3. **`Esc` shows "Install/uninstall is running."** `action_back` blocks
   with a notify when `self._running`. There's no cancel path. ☑ Issue
   1.2.A.
4. **Had to Ctrl+C the whole TUI.** Subprocess that never returns
   wedges the screen because there's no kill path. ☑ Issue 1.2.A.

---

## Part 2 — Testing-approach audit

The bugs above all existed pre-test-run. None of my 15 new tests
exercised the affected behaviour. The gap is structural.

### 2.1 What my new tests covered

Provider-level only:
- Row id presence
- Trailing-value string content
- Toggle getter return value
- Marker-file lifecycle

### 2.2 What my new tests *didn't* cover (and why each gap matters)

| Test paradigm | Why it would have caught a bug |
|---|---|
| Pilot-based screen-mount test | Mounting `ExtraDetailScreen` via `app.push_screen` + `pilot.pause` would have surfaced the missing chrome (no `#settings_box`) and the wrong CSS. |
| Keyboard-equivalence test for confirm screens | Asserting arrow-key navigation between Yes/Cancel in `CacheMaintenanceConfirm` and confirm dialogs (per your "arrows everywhere") would have caught any future regression in that pattern. |
| Drill-in integration test (root → leaf → back) | A test that opens settings, focuses Extras, presses Enter, asserts the resulting screen's `border_title` would have surfaced the inconsistent breadcrumb format. |
| Async loading-state test | A pilot test that mounts the Extras screen and asserts non-empty trailing values *after* `pilot.pause()` would have surfaced the blank-first-render. |
| Hint-bar content test | Asserting the contextual hint cluster on a new screen would have caught the missing `↑↓ Nav` cluster on `ExtraDetailScreen`. |
| Detail-strip content test | Asserting the bottom strip populates on cursor move would have caught the empty `description`/`hint` fields. |
| Cross-section search test | Asserting `/extras` finds the Indexing-rooted extras row would have caught any breadcrumb miswiring. |

The existing files
`tests/test_settings_p3_visual.py`,
`tests/test_settings_p3_search.py`,
`tests/test_settings_menu_p2.py`
already use these patterns. I had access to the gold-standard pattern and
ignored it.

### 2.3 Required additions to the test framework

These become required for any new settings screen — codified as a
checklist in `docs/test_patterns/settings_screen.md` (new file in the
plan below).

**Per new screen, before the screen lands:**

1. **Chrome-shape test.** `pilot.pause()`, mount the screen,
   `assert screen.query_one("#settings_box")`,
   `assert screen.border_title.startswith(expected_breadcrumb_prefix)`,
   `assert isinstance(screen, SettingsScreen) or screen has equivalent
   hint bar + detail strip`.

2. **Keyboard-equivalence test.** For every screen with selectable
   rows: `Up`/`Down` navigates, `Enter` activates, `Esc` pops.
   Confirm dialogs: arrows move between Yes/Cancel, only `Enter`
   selects (no spacebar / no auto-select).

3. **Hint-bar content test.** After mount, `screen._render_footer()`,
   `assert "Nav" in hint_bar_text and "Open" in hint_bar_text and "Back"
   in hint_bar_text`.

4. **Detail-strip content test.** For each selectable row, move the
   cursor to it, `pilot.pause()`, assert the `DetailStrip._description`
   isn't empty.

5. **Async loading test.** For any provider that touches the
   filesystem in `value_getter`, assert the row renders a placeholder
   on first paint and the real value after a deterministic refresh
   tick.

6. **Cross-section search test.** For each new searchable row,
   `screen.query_one("#settings_search", Input).value = "<keyword>"`,
   `await pilot.pause()`, assert the row appears in the filtered list
   and its breadcrumb is correct.

7. **Action-dispatch test.** For each `KIND_ACTION` row, monkeypatch
   the target `action_*` method, activate the row, assert the method
   ran exactly once.

**Per new sub-screen with destructive action:**

8. **Confirm-path test.** Push the confirm screen, navigate to "Yes,"
   press Enter, assert the destructive side-effect happened and the
   screen popped. Then a separate test for the "Cancel" path: navigate
   to "Cancel," press Enter, assert no side-effect.

**Per long-running action:**

9. **Modal lifecycle test.** Following `IndexerScreen` precedent — task
   lives on `FNDApp` not the screen; `Esc`/"Background" pops the screen
   but the task survives; reopening reattaches to the task's event
   queue; "Cancel" cleanly stops at next boundary.

---

## Part 3 — Target design (framework B)

### 3.1 Root menu (final)

```
Settings & Commands
─────────────────────────────────────────────────────
Preferences        Search · display · defaults · app defaults
Collections        4 collections · 7 sources
Indexing           pdf-structure · auto-resume · cache 1.2 GB
Keybindings        46 keys across 6 contexts
─────────────────────────────────────────────────────  (header divider)
↗ Open config file in editor          …/fnd/config.toml
↗ Open keybindings file in editor     …/fnd/keybindings.toml
```

The divider is a `KIND_HEADER` row with no label and an `↗ External`
section title. The leading glyph (`↗`) is rendered as part of the row
label, not via a new MenuItem field.

### 3.2 Indexing sub-screen

```
Settings & Commands › Indexing
─────────────────────────────────────────────────────
Structured PDF extraction
  Status                    Installed · ~900 MB
  Install / Uninstall…      (drill — opens disclosure + confirm)

Reindex behaviour
  Auto-resume on launch     On
  Cache size                1.2 GB · 4382 entries
  Cache maintenance…        (drill — prune / clear)
─────────────────────────────────────────────────────
[detail strip: contextual description for the selected row]
```

Notes:
- "Status" is a display-only row (no Enter action, value_getter only).
- "Install / Uninstall…" drills into a single screen whose body
  changes based on current installed state. Reuses the
  `DeleteCollectionScreen` confirm pattern.
- "Cache size" is display-only; "Cache maintenance…" drills into
  prune/clear actions.

### 3.3 Indexing → Install / Uninstall structured PDF extraction

```
Settings & Commands › Indexing › Install structured PDF
─────────────────────────────────────────────────────
pdf-structure — Structured PDF rendering (headings, lists,
tables, bold/italic, recovered image-rendered tables).

Will install:
  • pymupdf4llm[layout]            ~200 MB
  • docling-slim[standard]         ~700 MB

Approximate total disk: ~900 MB
ML model weights download on first use.

Indexing-time impact:
  ~30s per PDF on the first reindex (one-time per file;
  cached thereafter). 100 books ≈ 50 min; 500 books ≈ 4 h.
  Indexing runs in the background and auto-resumes.

  [ ✓ Install pdf-structure ]
  [   Cancel                ]
─────────────────────────────────────────────────────
↑↓ Nav · ⏎ Confirm · Esc Cancel
```

Chrome details:
- `SettingsScreen` subclass (NOT a bespoke `Screen`).
- Disclosure rendered as `Static`-with-Rich-Text rows above an
  `OptionList` of two options (matches `DeleteCollectionScreen`).
- Arrows move between the two options; Enter activates the
  highlighted option; Esc cancels.
- Running the install pushes the `IndexerScreen`-style modal pattern:
  a new `ExtrasInstallProgressScreen` that follows the *same* progress
  protocol as `IndexerScreen` (event queue on `FNDApp`, Background and
  Cancel bindings, progress bar drawn from
  ``asyncio.create_subprocess_exec`` stderr lines).
- The `_running` lock + Esc-notify mess is replaced by the modal
  lifecycle: cancel sends SIGTERM to the subprocess; background dismisses
  the modal but leaves the worker.

Uninstall mirror — same chrome, different button label, different
worker action.

### 3.4 Indexing → Cache maintenance

```
Settings & Commands › Indexing › Cache maintenance
─────────────────────────────────────────────────────
Prune stale entries…        4382 entries · 2 stale signatures
Clear cache…                4382 entries · 1.2 GB
─────────────────────────────────────────────────────
[detail strip: contextual description]
```

Each drill opens a `CacheMaintenanceConfirm` screen reused from current
implementation, but with chrome reset to match `DeleteCollectionScreen`:
- `border: round $error` (not `$warning`) for clear; `$warning` for prune
- Same internal layout: bordered box, summary `Static`, `OptionList` with
  Yes/Cancel, hint bar.

### 3.5 First-reindex disclosure (no menu surface)

The first-reindex warning modal stays. The "reset" affordance is
removed. To compensate, the install screen's disclosure (§3.3) now
includes the same cost narrative — so a user who installed via the
TUI never needs the warning modal; it only ever fires for the CLI
install path.

### 3.6 External-app row divider

A new `KIND_HEADER` row at the appropriate spot, no body text — the
existing rendering already paints headers in bold; we add a leading
`↗` glyph to the next two rows' labels. No new MenuItem field needed.

---

## Part 4 — Staged commit plan

Each step is small enough to verify independently. Each step ends with
`make lint && make test` green and the user-facing screen still
reachable.

### Step 1 — Rip out the broken pieces (1 commit)

**Goal:** clean slate. The current uncommitted diff is discarded
except for the parts in §1.1.

Files:
- `fnd/tui/menu.py` — revert all changes EXCEPT the `_make_reindex`
  routing change.
- `fnd/tui/settings_screen.py` — revert all additions
  (`ExtraDetailScreen`, `CacheInfoScreen`, `CacheMaintenanceConfirm`).
- `fnd/tui/actions.py` — drop the `reindex_default` Action.
- `tests/test_settings_extras_indexing_cache.py` — delete.
- `tests/test_settings_menu_p2.py` — revert label-list change.
- `docs/specs/2026-05-20-real-pdf-support-requirements.md` — drop
  F23–F26 (will be re-added per the rework).

**Keep:**
- `fnd/config.py` `indexer_auto_resume` field.
- `fnd/tui/app.py` direct read of `cfg.defaults.indexer_auto_resume`.
- `README.md` config path update (still correct under framework B).

Verification: `make lint && make test` green; root settings menu shows
the original 5 rows; nothing references the dropped Action.

### Step 2 — Test-pattern doc (1 commit, no code change)

Write `docs/test_patterns/settings_screen.md` codifying §2.3. Used by
every later commit's test addition.

### Step 3 — External-app row divider (1 commit)

Smallest UI change, lowest risk, validates the chrome pipeline before
the bigger sub-screens land.

Files:
- `fnd/tui/menu.py` — insert `header("External", level=2)` before the
  config-file rows; prefix their labels with `↗`.
- Tests: chrome-shape test asserts the header row exists; existing
  `test_root_menu_is_short_list_of_categories` label assertion
  updated.

### Step 4 — Indexing root + auto-resume toggle (1 commit)

The thinnest viable Indexing screen — just the auto-resume toggle and
a placeholder for what's coming.

Files:
- `fnd/tui/menu.py`:
  - Add `SECTION_INDEXING`, `_provider_indexing` (initial: header
    "Reindex behaviour", auto-resume toggle row).
  - Add Indexing root row to `_provider_root`.
  - Add Indexing to `_SECTION_PROVIDERS`/`_SECTION_LABELS`.
- Tests:
  - `test_root_menu_includes_indexing`
  - `test_indexing_auto_resume_round_trip` (pilot-based: open settings,
    drill Indexing, toggle row, assert config write).
  - Cross-section search: `/auto-resume` finds the row.

### Step 5 — Cache size display + maintenance drill (1 commit)

Files:
- `fnd/tui/menu.py`:
  - `_provider_indexing` gains "Cache size" (display) and "Cache
    maintenance…" (drill).
  - `_provider_cache_maintenance` lists prune / clear rows.
- `fnd/tui/settings_screen.py`:
  - `CacheMaintenanceConfirm` rewritten to inherit
    `DeleteCollectionScreen` chrome (same CSS, same bindings, same
    OptionList Yes/Cancel pattern). Arrows navigate; Enter activates;
    Esc cancels.
- Tests:
  - Chrome-shape test: confirm screen has `#settings_box` and the
    expected hint cluster.
  - Keyboard-equivalence test: arrows + enter + esc all behave per
    contract.
  - Prune/clear callback tests: monkeypatch the cleanup, navigate to
    Yes, press Enter, assert callback ran; separate test for the
    Cancel path.
  - Loading-state test: cache size renders a `…` placeholder on first
    paint and the real value after `pilot.pause()`.

### Step 6 — Structured-PDF install/uninstall screen (1-2 commits)

The big one. Split into:

**6a — install/uninstall confirm screen, no actual subprocess.**

Files:
- `fnd/tui/settings_screen.py`:
  - New `StructuredPdfConfirmScreen` (SettingsScreen-style) with
    disclosure body + OptionList Yes/Cancel.
- `fnd/tui/menu.py`:
  - `_provider_indexing` gains "Status" display row and "Install /
    Uninstall…" drill row.
- Tests:
  - Chrome-shape test for the confirm screen.
  - State-dependent body test: installed → uninstall copy; not
    installed → install copy.
  - Keyboard-equivalence test.

**6b — actual install/uninstall via `ExtrasInstallProgressScreen`.**

Files:
- New file `fnd/tui/extras_install_progress.py` — `ModalScreen`,
  follows `IndexerScreen` pattern. Owns an `asyncio.subprocess.Process`
  on FNDApp (`_extras_task`), event queue, progress events derived
  from stderr line counts (uv emits "Resolved N packages", "Installed
  N packages" lines we can pattern-match for a coarse progress bar).
  Background / Cancel bindings.
- `fnd/tui/app.py` — add `_extras_task`, `_extras_cancel`,
  `_extras_events` attrs alongside the existing `_indexer_*` ones.
- Tests:
  - Modal lifecycle test: mount, push, Esc dismisses but task
    survives; reopen finds the task; Cancel SIGTERMs and the task
    ends.
  - Subprocess test: monkeypatch `asyncio.create_subprocess_exec` with
    a controllable double; assert stderr lines flow into progress
    events.

### Step 7 — Cross-section search integration (1 commit)

`walk_all_sections` already iterates section providers — should pick
up the new Indexing rows automatically. Verify with tests; add
keyword tags (`extra`, `cache`, `prune`, `pdf-structure`,
`auto-resume`) so search latency matches the rest of the menu.

Tests:
- `/pdf-structure` finds the Install row.
- `/auto-resume` finds the toggle.
- `/cache` finds both Cache size and Cache maintenance.

### Step 8 — Requirements-matrix update + README (1 commit)

Re-add F23–F26 under the new IA. README "Settings → Indexing" section
replaces the current scattered notes.

---

## Part 5 — Acceptance criteria

When this plan lands, the user should be able to:

1. Open settings, see 5 categories + 2 external-app rows below a
   divider.
2. Drill into Indexing, see structured-PDF status + auto-resume +
   cache-size in one screen.
3. Toggle auto-resume from the menu — config field written, no other
   side-effects.
4. Install pdf-structure from the menu — disclosure + Yes/Cancel
   confirm, then a modal with progress, Cancel and Background work,
   subprocess can be SIGTERMed cleanly.
5. Prune or clear the cache — confirm dialog, arrows navigate Yes/
   Cancel, only Enter selects.
6. Press `/` from anywhere in settings, type `pdf` or `cache` or
   `auto-resume`, get the right row with a sensible breadcrumb.
7. Press `Esc` anywhere — pops one level; never blocks with a toast.

And the test suite should:

- Have a chrome-shape test per new screen.
- Have keyboard-equivalence tests for every confirm dialog.
- Have a loading-state test for any provider that touches the
  filesystem.
- Have a modal-lifecycle test for any long-running async work.

---

## Part 6 — Resolved calls

- **External-app glyph:** `↗`.
- **Cache "Clear" wording:** "Clear extraction cache…" with ellipsis;
  confirm dialog must make irreversibility unmissable (red border,
  bold "Cannot be undone" line, list of consequences in tight bullets).

## Part 7 — Cross-cutting UX rule (applies to every step)

**No walls of text. Glyphs, colour, and formatting do the work.**

Concretely:

- Every screen body reads as bullets / tight rows / aligned columns.
  Never a multi-line paragraph.
- Numbers and units get bold weight; explanatory clauses get dim.
- Status uses symbol + colour:
  `✓` success, `✗` error, `⚠` warning, `●` on, `○` off, `↗` external,
  `…` more-on-confirm, `⏎` enter, arrows for nav.
- Symbols must render in default macOS Terminal / iTerm fonts — no
  emoji, no Nerd-Font dependencies. The set above is safe.
- Disclosure copy is structured: header line, scannable bullet list,
  one-line cost summary, options. Not a wall of prose.
- Detail strip stays at 1 description line + 1 metadata line. No prose
  bleed.

Every PR in this plan is reviewed against this rule before merge.
