# UX pass 2 — Phase A implementation plan (layout & density)

**Goal:** Land the four density / layout fixes from the UX-pass-2 spec — drop the top bar, tighten padding, narrow the left column, fix the footer tint, surface location prefixes — each with a snapshot diff before/after and a single commit.

**Architecture:** No new modules. CSS edits inside `fnd/tui/app.py`'s class-level `CSS` string + a few label / compose changes. The `scripts/snap_tui.py` harness gives us before/after PNGs.

**Tech stack:** Textual 8.x (CSS, Tree, VerticalScroll), pytest, Rich.

---

## Task A1 — Remove the top status bar

**Files:**
- Modify: `fnd/tui/app.py` (drop `#status_bar` from compose + CSS + `_status_text`)
- Test: `tests/test_ux_a_pane_titles.py:test_status_bar_no_longer_duplicates_counts`

The active scope already lives in the Collections panel header; `fnd   scope: DPC` is one wasted row. The existing test pins "no counts in status bar" — generalise it to "no status bar at all".

- [ ] **Step 1: Update / write the failing test**

In `tests/test_ux_a_pane_titles.py`, replace `test_status_bar_no_longer_duplicates_counts` with:

```python
@pytest.mark.asyncio
async def test_status_bar_widget_is_removed(built_index: Path) -> None:
    """The top status bar is gone — its only content (active scope) is
    shown in the Collections panel border title instead. Pure dead pixels
    otherwise; lazygit has no equivalent strip."""
    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.query("#status_bar"), "status bar widget still mounted"
```

- [ ] **Step 2: Verify it fails**

Run: `uv run pytest tests/test_ux_a_pane_titles.py::test_status_bar_widget_is_removed -v`
Expected: FAIL with `status bar widget still mounted`.

- [ ] **Step 3: Drop the status bar**

In `fnd/tui/app.py`:

1. Remove `yield Static(self._status_text(), id="status_bar")` from `compose()`.
2. Remove `_status_text` method and the `#status_bar { … }` CSS rule.
3. Remove the `query_one("#status_bar", Static).update(…)` line from `_refresh_status`.

- [ ] **Step 4: Verify the test passes + nothing else broke**

```sh
uv run pytest -q
uv run ruff check fnd tests
uv run pyright
```

- [ ] **Step 5: Snap before/after**

```sh
PYTHONPATH=. uv run python scripts/snap_tui.py /tmp/fnd_a1.svg
rsvg-convert -o /tmp/fnd_a1.png /tmp/fnd_a1.svg
```

- [ ] **Step 6: Commit**

```sh
git add fnd/tui/app.py tests/test_ux_a_pane_titles.py
git commit -m "$(cat <<'EOF'
fix(tui): drop the top status bar

Active scope lives in the Collections panel header; the top row was
duplicate chrome.
EOF
)"
```

---

## Task A2 — Tighten padding & narrow the left column

**Files:**
- Modify: `fnd/tui/app.py` (CSS block)
- Snap: `scripts/snap_tui.py` (no change)

Lazygit's panels use `padding: 0 1` (1-col horizontal, no vertical) and a left:right ratio closer to 1fr:3fr. Today the preview is `padding: 1 2` and the column ratio is 1fr:2fr.

- [ ] **Step 1: Adjust the CSS block**

Find the `CSS = """ … """` block in `fnd/tui/app.py`. Replace with the version below — only the listed rules change; everything else (border colours, focus-within, scrollbars) stays.

```css
Screen { background: $surface; }
#query_bar { height: 1; padding: 0 1; }
#footer_hints { dock: bottom; height: 1; padding: 0 1; background: $surface; color: $text-muted; }
* { scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1; }
#results_column { width: 1fr; height: 1fr; }
#results_pane {
    width: 100%; height: 2fr;
    border: round $primary 50%;
    overflow-x: hidden;
}
#results_pane:focus-within { border: round $accent; }
#collections_panel_tree {
    width: 100%; height: 1fr;
    border: round $primary 50%;
    overflow-x: hidden;
}
#collections_panel_tree:focus-within { border: round $accent; }
#preview_pane {
    width: 3fr; height: 1fr;
    border: round $primary 50%;
    padding: 0 1;
}
#preview_pane:focus-within { border: round $accent; }
.preview-title { padding: 0 0 1 0; color: $accent; text-style: bold; }
.chunk-section { padding: 0 0 1 0; height: auto; }
.chunk-header { padding: 1 0 0 0; }
.chunk-line { padding: 0; height: auto; }
.chunk-line-match { background: $accent 8%; }
.chunk-section-focused { background: $accent 15%; }
#placeholder { color: $text-muted; }
#help_overlay { layer: overlay; background: $panel; border: round $accent; margin: 2 4 3 4; padding: 1 2 2 2; }
#cmd_palette { dock: bottom; height: 3; padding: 0 1; background: $panel; }
#collection_picker { layer: overlay; background: $panel; border: round $accent; margin: 4 8; padding: 1 2 2 2; height: auto; }
Tree > .tree--label { padding: 0 1; }
Tree > .tree--cursor { background: $accent 40%; color: $text; text-style: bold; }
```

Key changes only:
- `#query_bar` height 3 → 1 (drops the bordered Input box; uses a plain Input)
- `#preview_pane` width `2fr` → `3fr`, padding `1 2` → `0 1`
- `#footer_hints` background `$panel` → `$surface` (kills the pale-blue tint)
- `.chunk-line` padding `0 0 0 0` → `0` (cosmetic; same value)

- [ ] **Step 2: Verify the snapshot looks right**

```sh
PYTHONPATH=. uv run python scripts/snap_tui.py /tmp/fnd_a2.svg
rsvg-convert -o /tmp/fnd_a2.png /tmp/fnd_a2.svg
```

Open `/tmp/fnd_a2.png`. Expected: preview pane visibly wider, gaps inside boxes reduced, query bar a single row, footer no longer tinted.

- [ ] **Step 3: Run the full test sweep**

```sh
uv run pytest -q
uv run ruff check fnd tests
uv run pyright
```

- [ ] **Step 4: Commit**

```sh
git add fnd/tui/app.py
git commit -m "$(cat <<'EOF'
fix(tui): tighter pane padding and 1:3 left/right split

Match lazygit's density — preview claims more horizontal space, query
bar collapses to a single row, internal gaps drop.
EOF
)"
```

---

## Task A3 — Footer pale-blue tint fix

The `#footer_hints` rule already changes in A2 (`background: $surface`), removing the panel-tint highlight. The key glyphs keep their reverse style. No additional code change needed; covered by A2's snapshot.

If A2's snapshot still shows the tint, the cause is the `[reverse]` markup spilling onto the label. The fix:

- [ ] **Step 1: Inspect `_refresh_footer_hints` in `fnd/tui/app.py`** — confirm only `[reverse] {key} [/]` is wrapped, not the label.
- [ ] **Step 2: If the label is also styled, scope the markup more tightly** by writing key + label as separate `Text` runs:

```python
from rich.text import Text
hint = Text()
hint.append(f" {_format_key_hint(key)} ", style="reverse")
hint.append(f" {label}")
hints.append(hint)
```

(no Rich-markup parsing needed; explicit Text spans).

This task only fires if A2's snapshot still shows tint. Otherwise mark complete with no commit.

---

## Task A4 — Surface location prefixes on section rows

**Files:**
- Modify: `fnd/tui/app.py` (`_refresh_results_tree` — auto-expand top result)
- Modify: `fnd/tui/app.py` (`_format_hit_label` — fall back to `L<line>` when no heading)
- Test: new test in `tests/test_ux_a_pane_titles.py`

The user reported never seeing `§ heading` / `p.N` / `s.N` prefixes. Two reasons it might not be visible: (a) the user has to manually expand a file to see its sections, so they never see the section rows; (b) markdown chunks without an explicit `# Heading` fall back to `loc = "—"` in `_format_hit_label`.

- [ ] **Step 1: Failing test — top result should auto-expand**

Add to `tests/test_ux_a_pane_titles.py`:

```python
@pytest.mark.asyncio
async def test_top_result_is_auto_expanded(built_index: Path) -> None:
    """The first file row in the results tree should be expanded after a
    search so the user immediately sees its section rows (with their
    location prefixes) without having to press Right."""
    from textual.widgets import Tree

    app = FNDApp(index_dir=built_index, initial_query="blue penguin sandwich")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        first = next(iter(tree.root.children))
        assert first.is_expanded, "top result should auto-expand"
```

- [ ] **Step 2: Failing test — markdown fallback location**

```python
def test_format_hit_label_falls_back_to_line_marker_for_markdown() -> None:
    """When a markdown chunk has no heading_path / page / slide, the row
    should still carry a location marker — fall back to the chunk_seq as
    a synthetic 'chunk N' label."""
    from fnd.query import Hit
    from fnd.tui.app import _format_hit_label

    h = Hit(
        score=1.0, parent_id="x", path="/foo.md", kind="md",
        page=0, slide=0, heading_path="", title="", snippet="",
        chunk_seq=3, mtime=0, pass_index=0, meta_blob=b"",
    )
    label = str(_format_hit_label(h, max_score=10.0))
    assert "chunk 3" in label or "L" in label or "§" in label, label
```

- [ ] **Step 3: Verify both fail**

```sh
uv run pytest tests/test_ux_a_pane_titles.py -v
```

- [ ] **Step 4: Auto-expand top result in `_refresh_results_tree`**

In `fnd/tui/app.py`, find `_refresh_results_tree`. After the `for g in self._groups: …` loop, add:

```python
# Auto-expand the top result so its section rows are immediately
# visible — saves one keypress and makes the location prefix
# discoverable on first launch.
if tree.root.children:
    first = tree.root.children[0]
    first.expand()
```

- [ ] **Step 5: Add the markdown fallback in `_format_hit_label`**

Find the `else: loc = "—"` line. Replace with:

```python
else:
    # Markdown / TXT with no heading_path or page/slide — fall back
    # to the chunk sequence so the row still carries a locator.
    loc = f"chunk {h.chunk_seq + 1}"
```

- [ ] **Step 6: Verify both tests pass**

```sh
uv run pytest tests/test_ux_a_pane_titles.py -v
uv run pytest -q
```

- [ ] **Step 7: Snap & commit**

```sh
PYTHONPATH=. uv run python scripts/snap_tui.py /tmp/fnd_a4.svg
rsvg-convert -o /tmp/fnd_a4.png /tmp/fnd_a4.svg

git add fnd/tui/app.py tests/test_ux_a_pane_titles.py
git commit -m "$(cat <<'EOF'
fix(tui): surface section locators — auto-expand top result, label markdown chunks

Top result is expanded by default so the user sees § heading / p.N /
s.N prefixes without an extra keypress. Markdown chunks without an
explicit heading fall back to ``chunk N`` so the row still has a
locator.
EOF
)"
```

---

## Phase A close-out

After A1–A4 land:

- [ ] **Final snap** for visual review:

```sh
PYTHONPATH=. uv run python scripts/snap_tui.py /tmp/fnd_phase_a.svg
rsvg-convert -o /tmp/fnd_phase_a.png /tmp/fnd_phase_a.svg
```

- [ ] **Push** to origin once the user has eyeballed the screenshot:

```sh
git push origin main
```

- [ ] **Phase B** plan starts after the user signs off on the Phase A screenshot.
