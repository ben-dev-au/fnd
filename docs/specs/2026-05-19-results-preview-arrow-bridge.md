# Results ↔ preview arrow-bridge

Status: Approved → Implementing 2026-05-19

## Why

Once a search lands, the dominant flow is "pick a hit → read it in
the preview → maybe pick another". Today the keyboard path between
the two halves of the screen needs Tab (cycle) or `p` / `r`
(teleport actions). Arrow keys already drive everything inside each
pane, so the natural extension is to make ←/→ bridge the boundary
when the user has nowhere else to go on that key.

## Scope

Two touch points only:

* `FNDApp.action_tree_smart_expand` — `right` already runs through
  this for any focused tree. Extend the leaf branch.
* `MatchAwareScroll` (the `#preview_pane` widget) — `left` is
  currently bound to `scroll_left` by `VerticalScroll`. Override it.

No new action ids, no keymap changes, no schema bumps.

## Design

### 1. Right on a results leaf → preview

`action_tree_smart_expand` walks `_focused_tree()` and exits with a
no-op when the cursor is on a leaf (`not node.children`). Add a
single bridge inside that leaf branch:

```python
if node is None or not node.children:
    if node is not None and tree.id == "results_pane":
        self.action_focus_preview_pane()
    return
```

Only `#results_pane` bridges — the collections and filters panels
keep the existing leaf no-op so their leaves don't sling focus
across the screen.

The cursor stays parked on the hit row, the preview already shows
that hit, and the focus border lights up on the preview pane.

### 2. Left on the preview → results

`MatchAwareScroll` adds one widget-level `Binding("left",
"bridge_left", …)` that overrides the inherited `scroll_left`
binding. The action falls back to the original scroll when the pane
actually has horizontal content to reveal (`scroll_x > 0`); in the
common case (text wraps to width, `scroll_x == 0`) it focuses the
results tree:

```python
def action_bridge_left(self) -> None:
    if self.scroll_x > 0:
        self.scroll_left()
        return
    self.app.query_one("#results_pane").focus()
```

The results tree restores its previously-highlighted cursor row by
default — no extra state needed.

### 3. Loop

```
[Results: cursor on hit] --right--> [Preview: scrolling] --left--> [Results: cursor on hit]
                                          ^                              |
                                          +----- right ------------------+
```

Up / Down inside each pane retain their existing semantics
(navigate hits / scroll preview). The user can sweep through hits,
duck into the preview to read, and back out, all on the four arrow
keys.

## Tests

* Cursor on a leaf hit in `#results_pane`, press `right` →
  `#preview_pane` has focus.
* `#preview_pane` focused, press `left` → `#results_pane` has
  focus.
* Cursor on a *collapsed file row* in `#results_pane`, press
  `right` → still expands the row and drops the cursor on the
  first child (regression — the bridge only fires on real leaves).

## Risks

* Markdown / PDF previews that exceed the pane width could in
  principle want horizontal scroll. The `scroll_x > 0` fallback
  keeps `scroll_left` reachable — the user has to reach the
  rightmost edge first before Left bridges back.
* Filters / collections panel leaves are intentionally excluded.
  Their leaves are toggle rows, not preview anchors, so bridging
  to the preview from there would feel arbitrary.
