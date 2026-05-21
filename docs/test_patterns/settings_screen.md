# Settings-screen test pattern

Every new settings screen must ship with the tests below. The
provider-only tests added in the prior rip-out commit passed while the
UI was broken — that's the failure mode this checklist exists to
prevent.

Use `tests/test_settings_p3_visual.py` and `tests/test_settings_p3_search.py`
as reference implementations.

## Required tests per screen

### 1. Chrome shape

Mount the screen via `pilot`, assert the expected wrappers:

```python
async with app.run_test() as pilot:
    await pilot.pause()
    app.push_screen(MyScreen(...))
    await pilot.pause()
    screen = app.screen
    box = screen.query_one("#settings_box")
    assert box.border_title.startswith("Settings & Commands ›")
    assert screen.query_one("#footer_hints")
```

A screen without `#settings_box` and `#footer_hints` is a bug.

### 2. Keyboard equivalence

Every screen with selectable rows:

- `↑` / `↓` (and `k` / `j`) move the cursor.
- `Enter` activates the highlighted row.
- `Esc` (and `←`) pops back.
- No spacebar selection. No auto-confirm on focus.

For confirm dialogs (Yes / Cancel `OptionList`):

```python
await pilot.press("down")          # cursor on Cancel
await pilot.press("enter")         # selects Cancel
assert side_effect_did_not_happen
```

Repeat the inverse (`down` then `up`, Enter on Yes) in a separate test.

### 3. Hint-bar content

The contextual hint cluster reflects the screen:

```python
hint = screen.query_one("#footer_hints", Static)
plain = hint.renderable.plain  # rich Text
assert "Nav" in plain
assert "Back" in plain
```

For confirm screens: `assert "Confirm" in plain and "Cancel" in plain`.

### 4. Detail-strip content

For each selectable row, move the cursor onto it, `pilot.pause()`,
assert the `DetailStrip._description` is non-empty:

```python
lst = screen.query_one(SettingsList)
lst.cursor_index = i
await pilot.pause()
strip = screen.query_one(DetailStrip)
assert strip._description, f"row {i} has empty detail strip"
```

Empty detail strip = missing `description` / `hint` on the MenuItem.

### 5. Async loading state

For any `value_getter` that touches the filesystem or runs a
non-trivial query, first paint must show a placeholder (`…`), and the
real value lands on the next refresh:

```python
lst = screen.query_one(SettingsList)
row = lst._items[i]
assert row.trailing_value(app) == "…"   # first paint
await pilot.pause(0.5)
assert row.trailing_value(app) != "…"   # populated
```

The provider populates the trailing value via a worker that calls
`SettingsList.refresh_values()` when ready.

### 6. Cross-section search

For each new searchable row:

```python
search = screen.query_one("#settings_search", Input)
search.value = "<keyword>"
await pilot.pause()
labels = [it.label for it in screen.query_one(SettingsList)._items]
assert "<target row label>" in labels
```

The breadcrumb under the row should match the section the row lives in
(`Indexing` for indexing rows, not the section that *referenced* them).

### 7. Action dispatch

For each `KIND_ACTION` row:

```python
called = []
monkeypatch.setattr(app, "action_target_name", lambda: called.append(True))
lst = screen.query_one(SettingsList)
lst.cursor_index = idx_of_action_row
lst.action_activate()
assert called == [True]
```

For `KIND_EXTERNAL` rows that push a sub-screen, assert the right
screen type lands on top of `app.screen_stack`.

### 8. Confirm-path test (destructive actions only)

Each destructive action gets two tests:

- **Yes path**: navigate to Yes, press Enter, assert the side effect
  ran and the screen popped.
- **Cancel path**: navigate to Cancel, press Enter, assert no side
  effect and the screen popped.

A separate test exercises `Esc` from the confirm screen — same as
Cancel.

### 9. Modal-lifecycle test (long-running async work)

Follow `IndexerScreen` precedent:

- Task lives on `FNDApp`, not the screen.
- `Esc` / "Background" pops the modal; the task survives.
- Reopening the screen reattaches to the task's event queue.
- "Cancel" stops cleanly (subprocess work → `SIGTERM`, then `wait()`).
- After completion, the screen displays a terminal state (`✓` / `✗`)
  and the task is released from the app.

```python
await pilot.press("escape")
assert app._extras_task is not None       # task still running
app.push_screen(ExtrasInstallProgressScreen())
await pilot.pause()
# reattached — same task, fresh widget
```

## UI rule (cross-cutting)

Plus the visual contract from `docs/plans/2026-05-21-settings-menu-rework.md` (v3):

- No prose paragraphs in screen bodies.
- Numbers / units bold; explanation dim.
- Status uses symbol + colour: `✓ ✗ ⚠ ↗ … ⏎ ↑ ↓ ← → ▸ ▾`.
- Symbols must render in default macOS Terminal / iTerm fonts. No Nerd Fonts.

### Row-kind visual language

Each kind gets a distinct colour + glyph pairing — verified by direct
unit test against `fnd.tui.settings_screen._trailing_segments` (see
`tests/test_settings_visual_language.py`):

| Kind | Trailing | Colour |
|---|---|---|
| Toggle | `✓ on` / `✗ off` | `bold green` / `bold red` |
| Action | `[ Run ]` or `[ Run… ]` | `bold cyan` (accent) |
| Sub-menu drill | trailing `▸` | `bold cyan` |
| External drill | summary (dim) + `▸` | mixed |
| External app | dim summary; leading `↗` on label | `bold cyan` arrow |
| Picker | `value ▾` | bright value + accent caret |
| Scalar | bare `value` (bold) | `bold` |
| Display | bare `value` (bold); **label rendered dim** | `dim` label · `bold` value |

Adding a new kind: add a branch to `_trailing_segments` and a row to
the table here. Then add a parametrised case to
`tests/test_settings_visual_language.py`.

### Hint bar accuracy

The `⏎` label in the footer hint must match what Enter does on the
focused row. Implementation: `_hint_cluster()` inspects
`cursor_item.kind` and rewrites the verb (Toggle / Edit / Choose /
Open / Run / Open in editor / — for display). Test pattern lives in
`tests/test_settings_hint_per_kind.py`.

A footer that says "Open" while Enter actually toggles is a bug — the
test catches it.

### Async loading

Any `value_getter` that walks the filesystem must route through
`fnd.tui.lazy_trailing.get_or_schedule`. First render returns `…`;
the worker populates the real value and re-renders the screen.

Tests assert (`tests/test_settings_async_loading.py`):
- First call returns the `PLACEHOLDER`.
- Background worker populates the cache.
- `invalidate(key)` forces recomputation.

`on_screen_resume` invalidates relevant keys so reopening the screen
recomputes.

A screen that fails any rule above is a bug even if every test above
passes.

## When this checklist applies

Any new file under `fnd/tui/` whose class inherits `Screen` or
`ModalScreen` and is reachable from the settings menu.

The reverse also applies: removing a screen retires its tests.
