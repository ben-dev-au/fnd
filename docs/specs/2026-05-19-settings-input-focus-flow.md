# Settings menu: open-focus the filter input + arrow-bridge between input and list

Status: Approved → Implementing 2026-05-19

## Why

Two small UX gaps in the Settings screen:

1. On open, the SettingsList gets focus, so typing does nothing
   until the user presses `/` to focus the filter. Two-step
   discovery for the dominant interaction (filter then activate).
2. Arrow keys don't bridge the input ↔ list boundary. Once the
   filter has focus, the user must use Enter or `/` to leave it;
   from the list, there's no arrow path back to the input.

## Scope

Single screen: :class:`fnd.tui.settings_screen.SettingsScreen` and
its child :class:`SettingsList`. No new actions, no config knobs,
no schema changes.

## Design

### 1. Focus on open

`SettingsScreen.on_mount` currently calls `lst.focus()`. Change to
focus the search Input. The list still seeds its cursor on
``_init_cursor``; only the focused widget changes.

```python
def on_mount(self) -> None:
    lst = self.query_one(SettingsList)
    lst.set_items(list(self._items))
    self.query_one("#settings_search", Input).focus()
    self._render_footer()
    ...
```

### 2. Down from input → list

Add a key handler that captures `down` while the search Input has
focus and transfers focus to the SettingsList:

```python
@on(events.Key, "#settings_search")
def _on_search_key(self, ev: events.Key) -> None:
    if ev.key == "down":
        self.query_one(SettingsList).focus()
        ev.stop()
        ev.prevent_default()
```

The list's `cursor_index` is preserved — whichever row was
highlighted stays highlighted after the focus switch.

### 3. Up from top of list → input

Modify `SettingsList.action_move(-1)`. When the cursor is at the
topmost selectable index and Up is requested, transfer focus to
the screen's search Input instead of clamping:

```python
def action_move(self, delta: int) -> None:
    if delta == -1 and self._is_at_top():
        try:
            self.screen.query_one("#settings_search", Input).focus()
        except Exception:
            pass
        return
    ...  # existing move logic
```

`_is_at_top()` returns True when `cursor_index == _first_selectable(0, +1)`.

### 4. Quality-of-life consequences

* 1-9 jumps still fire on the list (Input absorbs digits as filter
  text — the right trade-off, since filtering is the dominant
  intent of a focused input).
* `Esc` from input: existing `action_back` handles "clear filter
  if non-empty, else pop screen". Unchanged.
* `Enter` from input: existing `_on_search_submitted` focuses
  the list and activates the first match. Unchanged.
* `/` from list focus: still focuses the search Input. Redundant
  on first open, useful after the user has navigated away.

## Tests

* `screen.focused` is the search Input immediately after mount.
* Pressing `down` from the Input focuses the SettingsList.
* Pressing `up` while the list's cursor sits at the topmost
  selectable index focuses the Input (cursor stays where it was).
* Typing a single letter immediately filters the list — no prior
  focus shift required.
* Regression: pressing `up` while the cursor is NOT at the top
  still moves the cursor up one row (does not jump straight to
  the input).

## Risks

* Headers occupy index 0 in many menus; `_first_selectable` may
  return index 1+. The "at top" check must use the first
  *selectable* index, not 0, otherwise Up at the first row would
  no-op instead of bridging.
