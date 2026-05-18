# Fuzzy search: user-configurable toggle, min-chars floor, runtime shortcut

Status: Draft → Implementing 2026-05-19

## Why

Fuzzy matching today is hardcoded: the cascade fallback always runs the
fuzzy pass (`fnd/cascade.py:104`) with per-term distances chosen by
Lucene's AUTO heuristic in `auto_fuzzy_distance` (`fnd/matching.py:44`).
Two pieces of user agency are missing:

1. **No way to turn it off.** Users who want strict exact/phrase matches
   have no escape hatch short of writing tantivy operators (and
   `term~1` doesn't currently route into the cascade pass anyway —
   see §5 below).
2. **No min-term-chars floor.** The AUTO heuristic already gives
   distance 0 for stems of length ≤ 2, but users want to raise that
   floor (e.g. "no fuzzy on terms shorter than 5 chars") to suppress
   spurious matches from short stems.

## Scope

In:

* New `defaults.fuzzy_enabled: bool = True` and
  `defaults.fuzzy_min_term_chars: int = 3` config fields.
* Cascade pass and highlighter respect both knobs.
* Explicit per-term `~N` syntax in the query overrides the auto-fuzzy
  off state — a user can disable auto-fuzzy globally and still opt in
  per-term.
* Settings-menu rows for both fields (Preferences section).
* TUI action + default binding to toggle `fuzzy_enabled` from the
  query bar; persists across sessions by writing to the config TOML
  through the existing `write_setting()` path.
* `notify()` feedback when the toggle fires.

Out:

* A persistent visible indicator (chip / status badge). The toggle
  notification is enough for v1; revisit if it feels under-discoverable.
* Strength dials beyond on/off. The AUTO heuristic stays the default
  distance behavior. The min-chars floor is the only other knob.
* Wiring `~N` into the strong-signal or fusion regimes. It only flows
  through the cascade fuzzy pass — same as the auto-fuzzy behavior.

## Design

### 1. Config schema (`fnd/config.py`)

Add to `Defaults`:

```python
fuzzy_enabled: bool = True
fuzzy_min_term_chars: int = 3
```

`fuzzy_min_term_chars` is the minimum post-stem length for auto-fuzzy
to apply. Values 0-3 are no-ops vs current behavior because
`auto_fuzzy_distance` already returns 0 for stems of length ≤ 2.
Values 4+ extend the floor.

`CONFIG_TEMPLATE` gets two new lines under `[defaults]` with one-line
comments — same style as the existing knobs.

### 2. Cascade fuzzy pass (`fnd/cascade.py`)

New helper:

```python
def _terms_with_fuzzy(query: str) -> list[tuple[str, int | None]]:
    """Like _terms_from_query, but preserves per-term ~N modifiers.
    Returns (raw_term, explicit_distance | None) tuples. A bare ``~``
    (no number) is treated as None — caller falls back to AUTO."""
```

The parser strips operators / field qualifiers / range syntax (same
as `_terms_from_query`), but for each surviving token matches an
optional trailing `~\d` modifier. Phrase-proximity (`"a b"~N`) is
excluded because quoted phrases are stripped before per-token
parsing. Bare `~` with no digit is not recognized as a fuzzy opt-in
(the `~` is dropped, term treated as exact). `~N` is clamped to
`{1, 2}` (tantivy's distance cap).

`_fuzzy_pass` gains two parameters:

```python
def _fuzzy_pass(
    searcher, *, query, limit, collection,
    active_sources=None, intent=None,
    auto_fuzzy_enabled: bool = True,
    min_term_chars: int = 0,
) -> list[Hit]: ...
```

Per-term distance resolution becomes:

```python
def _resolve_distance(stem: str, explicit: int | None) -> int:
    if explicit is not None:
        return min(explicit, 2)
    if not auto_fuzzy_enabled:
        return 0
    if len(stem) < min_term_chars:
        return 0
    return auto_fuzzy_distance(stem)
```

Early-bail change: if *every* resolved distance is 0, return `[]`
without issuing the boolean query. This makes "auto-fuzzy off, no
explicit ~N" a true no-op for the cascade.

`cascade_search` and `search_layered` thread both params through to
`_fuzzy_pass`. Defaults preserve current behavior so existing tests
keep passing.

### 3. Highlighter (`fnd/matching.py`)

Rename `MatchSpec.from_query`'s `fuzzy: bool = True` to
`auto_fuzzy: bool = True` and add `min_term_chars: int = 0`. Update
all callers (TUI app.py:1554 uses `fuzzy=True`).

Body change:

* Stems below `min_term_chars` are skipped when building
  `fuzzy_per_stem`.
* When `auto_fuzzy=False`, the AUTO-derived fuzzy pairs are dropped.
* Explicit-`~N` terms parsed out of the query are always added to
  `fuzzy_per_stem` at the user's distance, regardless of
  `auto_fuzzy`.

Without this change, toggling fuzzy off would still paint fuzzy
variants in the preview — a visible drift between match semantics
and highlight semantics.

### 4. TUI action + binding (`fnd/tui/actions.py`, `fnd/tui/app.py`)

New action:

```python
Action(
    id="toggle_fuzzy",
    description="Toggle auto-fuzzy matching on or off. "
                "Persists to the config TOML.",
    default_key="ctrl+t",
    command="fuzzy",
    footer_label="Fuzzy",
    show_in_footer=False,
)
```

`ctrl+t` (mnemonic: toggle) because Textual's Input widget owns most
common ctrl-combos (ctrl+a/c/d/e/f/k/u/v/w/x). ctrl+t isn't among
them, so it bubbles up to the app-level binding even when the query
bar has focus. Users can rebind via `keybindings.toml`.

`FNDApp.action_toggle_fuzzy`:

1. Compute the new value (`not self._config.defaults.fuzzy_enabled`).
2. Call `write_setting(config_path=..., dotted_path="defaults.fuzzy_enabled", value=new)`.
3. Reassign `self._config` from the returned `Config`.
4. `self.notify(f"Fuzzy: {'on' if new else 'off'}")`.
5. If the query bar is non-empty, re-run the current search so the
   change is visible without an extra keystroke.

### 5. Settings menu (`fnd/tui/menu.py`)

Two new rows in the Preferences section:

* **Auto-fuzzy matching** — bool toggle backed by
  `defaults.fuzzy_enabled`.
* **Auto-fuzzy minimum term length** — integer picker (0-10), backed
  by `defaults.fuzzy_min_term_chars`.

Both follow the existing `setting_path` + `picker_setter` pattern
used for the other numeric and bool knobs in the menu.

### 6. CLI

`fnd search` reads `defaults.fuzzy_enabled` and
`defaults.fuzzy_min_term_chars` from `load()` and threads them into
`search_layered`. No new CLI flags — config is the surface for now.

## Tests

* **Config round-trip** (`tests/test_config_*.py`): new defaults,
  values clamp to documented ranges (or not — validators are
  optional; min-chars is a soft floor, not a clamp).
* **Cascade behavior** (new `tests/test_fuzzy_toggle.py`):
  * Default config: existing cascade fuzzy tests still pass.
  * `auto_fuzzy_enabled=False`: `_fuzzy_pass` returns `[]` for a query
    with no `~N`.
  * `auto_fuzzy_enabled=False` + query `"templat~1"`: pass runs with
    distance 1, finds the AUTO-equivalent variants.
  * `min_term_chars=5`: a 4-char stem gets distance 0 even with
    auto-fuzzy on.
* **Highlighter** (extend `tests/test_highlight_covers_fuzzy_and_synonyms.py`):
  * `auto_fuzzy=False`: AUTO variants aren't painted.
  * `auto_fuzzy=False` + explicit `~N`: that term's variants *are*
    painted.
* **TUI action**: action invokes `write_setting`, re-loads config,
  emits notification, re-runs the in-flight search.
* **Settings menu**: round-trip both fields through the UI helpers.

## Risks

* **Renaming `MatchSpec.from_query`'s `fuzzy=` kwarg** changes a
  public-ish API. The only caller is `app.py:1554`; tests use the
  factory by name. Sweep all callers.
* **`ctrl+f` collisions:** terminals occasionally bind ctrl+f to
  forward-page-scroll or similar. Document the rebind path in the
  notification when first triggered? — defer; user feedback will
  tell.
* **Re-running the search on toggle** could surprise users mid-edit.
  Mitigate by only re-running when the query bar is non-empty *and*
  not currently focused-with-pending-debounce. If pending debounce,
  let it fire naturally with the new setting on next keystroke.
