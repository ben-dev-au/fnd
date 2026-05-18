# Settings Menu Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the settings menu up to the UX described in `docs/specs/2026-05-11-settings-menu-redesign.md` — search-first root with informative rows, cross-section search from any screen, single-screen Add Collection wizard with multi-select pickers, lazygit-style press-key-to-invoke on Keybindings, reveal-in-Finder pattern, and user-configurable drill-cue style.

**Architecture:** Modify the existing `acorn.tui.menu` / `acorn.tui.settings_screen` / `acorn.tui.app` / `acorn.config` modules in place. Add one new widget file (`acorn/tui/widgets/detail_strip.py`) and one new opener helper (`acorn.opener.reveal`). All cross-section search and row rendering stays inside `SettingsScreen` so the menu data model (`menu.py`) remains pure data.

**Tech Stack:** Python 3.13 · Textual ≥ 0.85 · Rich · Pydantic · `pytest` + `pytest-asyncio` + `pytest-textual-snapshot` · `tomlkit` for comment-preserving writes.

---

## Process guardrails (do not skip)

Drift, not lack of plan, was what broke the last iteration. Treat these as non-negotiable for every task:

1. **Phase gates.** This plan is five phases. After every phase, post a "done vs spec" diff against `docs/specs/2026-05-11-settings-menu-redesign.md` and **stop for explicit user sign-off** before starting the next phase. The phases are sized so a single sign-off is meaningful, not a rubber-stamp.
2. **Spec-anchored tests.** Every test docstring opens with a `Spec:` line citing the spec section it covers. Example:
   ```python
   """Spec: Information architecture › Root — cursor lands on first selectable row."""
   ```
   When a verification item in the spec has no anchored test, that's a gap — flag it.
3. **No silent omissions.** If a spec item turns out not to fit during implementation, **stop** and surface the deviation before changing direction. Default: the spec wins. Any deviation needs explicit OK before the code lands.
4. **Small, spec-numbered commits.** Each commit message references the task and the spec section it's implementing (e.g. `feat(settings): cross-section search walker (Phase 2 · Task 6 · spec §Search behaviour)`). Easier to audit, easier to revert one piece without unwinding the rest.

---

## File map (locks in decomposition)

| File                                                | Responsibility                                                                                              | Phase introducing change |
|-----------------------------------------------------|-------------------------------------------------------------------------------------------------------------|--------------------------|
| `acorn/tui/widgets/__init__.py` *(new)*             | Re-exports `DetailStrip`.                                                                                    | 1                        |
| `acorn/tui/widgets/detail_strip.py` *(new)*         | `DetailStrip(Widget)` — the 2-line description + metadata area docked at the bottom of every settings screen. | 1                        |
| `acorn/tui/settings_screen.py`                      | All settings screens, the bottom edit bar, the row renderer, the cross-section search, the wizard.            | 1–5                      |
| `acorn/tui/menu.py`                                 | Menu data model, providers, kinds, the cross-section walker.                                                 | 1, 2, 3, 4, 5            |
| `acorn/tui/app.py`                                  | Bindings, hint bar variants, the `action_open_keybindings_file` action, `_close_settings_stack` adjustments. | 1, 4                     |
| `acorn/config.py`                                   | `INDEXER_FILETYPES` constant; new `Defaults.drill_summary_mode` field; updated `CONFIG_TEMPLATE`.            | 3, 5                     |
| `acorn/opener.py`                                   | New `reveal(path)` helper.                                                                                   | 4                        |
| `tests/test_settings_p3_visual.py` *(new)*          | Phase 1 visual tests.                                                                                        | 1                        |
| `tests/test_settings_p3_search.py` *(new)*          | Phase 2 cross-section search tests.                                                                          | 2                        |
| `tests/test_settings_p3_wizard.py` *(new)*          | Phase 3 Add Collection wizard tests.                                                                         | 3                        |
| `tests/test_settings_p3_reveal.py` *(new)*          | Phase 4 reveal-in-Finder & open-keybindings-file tests.                                                      | 4                        |
| `tests/test_settings_p3_keybindings_invoke.py` *(new)* | Phase 5 press-key-to-invoke + drill-cue preference tests.                                                  | 5                        |

The existing `tests/test_settings_menu_p2.py` and `tests/test_actions_keymap.py` stay; their assertions update as IA shifts within the phases.

---

## Phase 1 — Visual foundation

Make the menu look right. Container hugs content. Detail strip at the bottom of every screen. Row anatomy supports labels + drill summaries + setting values + bracketed keys. F3 dropped.

This is the phase that should make the root *stop reading as empty* even before any other work lands.

### Task 1 — `INDEXER_FILETYPES` constant lives in `acorn.config`

**Files:**
- Modify: `acorn/config.py`
- Test: `tests/test_settings_p3_visual.py` (new)

Used in Phase 3 by the wizard; introduced here because it's a single-source-of-truth constant unrelated to the rest of Phase 1 work. Doing it first means later tasks can import it freely.

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_p3_visual.py`:

```python
"""Phase 3 (Settings UX redesign) — visual foundation tests."""

from __future__ import annotations


def test_indexer_filetypes_exposed_and_complete() -> None:
    """Spec: Add Collection wizard › Includes — file types come from a
    single source of truth, not hardcoded in two places."""
    from acorn.config import INDEXER_FILETYPES

    # Map of extension -> human label. Order is the order the picker shows.
    assert tuple(INDEXER_FILETYPES) == ("md", "pdf", "docx", "pptx", "txt")
    assert INDEXER_FILETYPES["md"] == "Markdown (.md)"
    assert INDEXER_FILETYPES["pdf"] == "PDF (.pdf)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_indexer_filetypes_exposed_and_complete -v`
Expected: FAIL with `ImportError: cannot import name 'INDEXER_FILETYPES' from 'acorn.config'`.

- [ ] **Step 3: Add the constant**

Edit `acorn/config.py`. After the imports and before `class SourceConfig`, add:

```python
# Indexer-supported file types in display order. Used by the Add Source /
# Add Collection wizards to render the Includes multi-select. Keep this in
# sync with the kinds the extractor pipeline handles.
INDEXER_FILETYPES: dict[str, str] = {
    "md": "Markdown (.md)",
    "pdf": "PDF (.pdf)",
    "docx": "Word (.docx)",
    "pptx": "PowerPoint (.pptx)",
    "txt": "Plain text (.txt)",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_indexer_filetypes_exposed_and_complete -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/config.py tests/test_settings_p3_visual.py
git commit -m "feat(config): expose INDEXER_FILETYPES (Phase 1 · Task 1 · spec §Add Collection)"
```

---

### Task 2 — Drop the F3 binding

**Files:**
- Modify: `acorn/tui/actions.py` (remove `default_key="f3"` from `open_collections_form`)
- Test: `tests/test_settings_p3_visual.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_visual.py`:

```python
def test_f3_no_longer_in_keymap() -> None:
    """Spec: Locked decisions — F3 dropped."""
    from acorn.tui.actions import load_keymap

    keymap = load_keymap()
    assert "f3" not in keymap.bindings, (
        f"F3 should not be bound; keymap.bindings has: {keymap.bindings.get('f3')!r}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_f3_no_longer_in_keymap -v`
Expected: FAIL — F3 is currently bound to `open_collections_form`.

- [ ] **Step 3: Remove F3 from the registry**

In `acorn/tui/actions.py` find the `open_collections_form` Action and change `default_key="f3"` to `default_key=None`:

```python
Action(
    id="open_collections_form",
    description="Open the Collections form (add / edit / delete collections).",
    default_key=None,
    command="collections-form",
    footer_label="Manage",
    show_in_footer=False,
),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_f3_no_longer_in_keymap -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/actions.py tests/test_settings_p3_visual.py
git commit -m "feat(keymap): drop F3 binding (Phase 1 · Task 2 · spec §Locked decisions #9)"
```

---

### Task 3 — `DetailStrip` widget

**Files:**
- Create: `acorn/tui/widgets/__init__.py`
- Create: `acorn/tui/widgets/detail_strip.py`
- Test: `tests/test_settings_p3_visual.py`

This widget gets mounted at the bottom of every settings screen (inside the bordered box). Empty by default; populated by the screen calling `strip.set(description, metadata)` when the cursor row changes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_visual.py`:

```python
def test_detail_strip_renders_description_and_metadata() -> None:
    """Spec: Visual system › Detail strip — 2 lines, description then
    metadata in $text-muted."""
    from acorn.tui.widgets.detail_strip import DetailStrip

    strip = DetailStrip()
    strip._description = "Result limit (1–1000) — max results returned per query."
    strip._metadata = "Stored in defaults.result_limit · Applies on next search"
    rendered = strip._render_lines()
    assert len(rendered) == 2
    assert "Result limit" in str(rendered[0])
    assert "Stored in defaults.result_limit" in str(rendered[1])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_detail_strip_renders_description_and_metadata -v`
Expected: FAIL — module doesn't exist yet.

- [ ] **Step 3: Create the widgets package init**

Create `acorn/tui/widgets/__init__.py`:

```python
"""TUI widgets shared across settings screens."""

from acorn.tui.widgets.detail_strip import DetailStrip

__all__ = ["DetailStrip"]
```

- [ ] **Step 4: Create the DetailStrip widget**

Create `acorn/tui/widgets/detail_strip.py`:

```python
"""DetailStrip — 2-line description + metadata area at the bottom of
every settings screen.

Empty by default. The parent screen calls ``set(description, metadata)``
on cursor row changes; ``clear()`` blanks it. Uses Rich Text so the
metadata line gets $text-muted styling and the description line stays
plain $text.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class DetailStrip(Widget):
    """A two-line dim area docked at the bottom of a settings container.

    Line 1: row description in $text.
    Line 2: metadata (storage path, range, applicability note) in $text-muted.
    Separated from the row list above by the container's own thin rule.
    """

    DEFAULT_CSS = """
    DetailStrip { height: 3; padding: 1 0 0 0; }
    DetailStrip > Static { height: 1; padding: 0 1; }
    DetailStrip > Static.-description { color: $text; }
    DetailStrip > Static.-metadata { color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._description: str = ""
        self._metadata: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", classes="-description", id="detail_description")
        yield Static("", classes="-metadata", id="detail_metadata")

    def set(self, description: str, metadata: str = "") -> None:
        self._description = description
        self._metadata = metadata
        self._refresh_strip()

    def clear(self) -> None:
        self.set("", "")

    def _refresh_strip(self) -> None:
        # Internal name to avoid colliding with Widget._render (Textual base).
        rendered = self._render_lines()
        try:
            self.query_one("#detail_description", Static).update(rendered[0])
            self.query_one("#detail_metadata", Static).update(rendered[1])
        except Exception:
            pass

    def _render_lines(self) -> tuple[Text, Text]:
        """Pure render — tested directly without mounting the widget."""
        return (
            Text(self._description) if self._description else Text(""),
            Text(self._metadata, style="dim") if self._metadata else Text(""),
        )

    def on_mount(self) -> None:
        self._refresh_strip()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_detail_strip_renders_description_and_metadata -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/widgets/ tests/test_settings_p3_visual.py
git commit -m "feat(tui): DetailStrip widget (Phase 1 · Task 3 · spec §Visual system › Detail strip)"
```

---

### Task 4 — Bracketed `[key]` rendering in the row renderer

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`_render_row` function)
- Test: `tests/test_settings_p3_visual.py`

Keys today render as plain `key.ljust(12)` in dim. Per spec they should render bracketed in `$accent`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_visual.py`:

```python
def test_row_with_key_renders_bracketed_accent() -> None:
    """Spec: Visual system › Key style — bracketed `[o]` accent."""
    from acorn.tui.menu import KIND_ACTION, MenuItem
    from acorn.tui.settings_screen import _render_row

    item = MenuItem(
        id="k.test",
        label="Open at locator",
        kind=KIND_ACTION,
        key="o",
        action_id="open_at_locator",
    )
    rendered = _render_row(item, app=None, width=80)
    text_str = str(rendered)
    assert "[o]" in text_str, f"expected '[o]' in rendered row; got: {text_str!r}"
    assert "▶" not in text_str
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_row_with_key_renders_bracketed_accent -v`
Expected: FAIL — current renderer outputs raw `o` not `[o]`.

- [ ] **Step 3: Update `_render_row` in `settings_screen.py`**

Find the existing `_render_row` function. Replace the section that handles `item.key` rendering:

```python
def _render_row(item: MenuItem, app: AcornApp | None, width: int | None = None) -> Text:
    """Render one menu row as Rich Text.

    Layout (left to right):
      [key]  label  ……………… trailing_value

    - Keys (Keybindings rows) render as ``[<key>]`` in $accent bold,
      bracketed in $text-muted for a subtle frame.
    - Labels render in $text.
    - Trailing values right-align in $primary bold (setting values) or
      $text-muted italic (drill row summaries / search breadcrumbs).
    """
    if item.kind == KIND_HEADER:
        return _render_header(item, width)

    text = Text()
    if item.key:
        # Bracketed key in 12-char column: "[<key>]" + padding.
        bracket_open = Text("[", style="dim")
        key_glyph = Text(item.key, style="$accent bold")
        bracket_close = Text("]", style="dim")
        key_field = bracket_open + key_glyph + bracket_close
        # Pad to 12 columns so labels align across rows.
        used = len(item.key) + 2  # brackets + key
        key_field.append(" " * max(1, 12 - used))
        text.append_text(key_field)
    text.append(item.label)
    trailing = item.trailing_value(app) if app is not None else ""
    if trailing and width is not None:
        used = (12 if item.key else 0) + len(item.label)
        pad = max(2, width - used - len(trailing) - 2)
        text.append(" " + "·" * pad + " ", style="dim")
        text.append(trailing, style="bold")
    elif trailing:
        text.append("   ")
        text.append(trailing, style="bold")
    return text
```

Note: `app=None` path is for tests that don't construct a full app — `trailing_value` is just skipped in that case.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_row_with_key_renders_bracketed_accent -v`
Expected: PASS.

- [ ] **Step 5: Run existing tests to make sure nothing regressed**

Run: `uv run pytest tests/test_settings_menu_p2.py tests/test_actions_keymap.py -v`
Expected: PASS (some Keybindings cursor-skip tests touch the renderer, but they assert on `KIND_HEADER` skipping which is unaffected).

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_visual.py
git commit -m "feat(settings): bracketed [key] rendering (Phase 1 · Task 4 · spec §Visual system › Key style)"
```

---

### Task 5 — Container hugs content (`height: auto`, centered, max-width)

**Files:**
- Modify: `acorn/tui/settings_screen.py` (CSS block on `SettingsScreen`)
- Test: `tests/test_settings_p3_visual.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_visual.py`:

```python
def test_root_container_hugs_content() -> None:
    """Spec: Visual system › Container — height: auto, not 1fr."""
    from acorn.tui.settings_screen import SettingsScreen

    css = SettingsScreen.CSS
    assert "height: auto" in css
    # The screen previously had `height: 1fr` on #settings_box.
    # It must not anymore (the box should hug content).
    assert "height: 1fr;" not in css.replace("height: 1fr;", "", 1) or "1fr" not in css.split("#settings_box")[1].split("}")[0]
```

The second assertion is awkward — simpler:

```python
def test_root_container_hugs_content() -> None:
    """Spec: Visual system › Container — height: auto, not 1fr."""
    from acorn.tui.settings_screen import SettingsScreen

    css = SettingsScreen.CSS
    # Find the #settings_box rule and check its height.
    box_rule = css.split("#settings_box {")[1].split("}")[0]
    assert "height: auto" in box_rule
    assert "max-height" in box_rule
    assert "align: center middle" in css  # somewhere in the screen styles
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_root_container_hugs_content -v`
Expected: FAIL — current CSS has `height: 1fr`.

- [ ] **Step 3: Update `SettingsScreen.CSS`**

Replace the existing CSS block with:

```python
    CSS = """
    SettingsScreen { background: $surface; align: center middle; }
    SettingsScreen > #settings_box {
        height: auto;
        max-height: 90%;
        width: auto;
        min-width: 60;
        max-width: 100;
        border: round $primary 50%;
        padding: 0 1;
    }
    SettingsScreen > #settings_box:focus-within { border: round $accent; }
    #settings_search {
        height: 1; padding: 0 0; border: none; background: $surface; color: $text;
    }
    #settings_search:focus { color: $accent; }
    SettingsScreen > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    SettingsScreen > #settings_status {
        dock: bottom; height: 1; padding: 0 1; color: $text-muted; background: $surface;
    }
    """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_root_container_hugs_content -v`
Expected: PASS.

- [ ] **Step 5: Run existing settings tests for regression**

Run: `uv run pytest tests/test_settings_menu_p2.py -v`
Expected: PASS — these tests check behaviour, not sizing.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_visual.py
git commit -m "feat(settings): container hugs content, centered (Phase 1 · Task 5 · spec §Visual system › Container)"
```

---

### Task 6 — Live trailing summaries on root drill rows + DetailStrip mounted

**Files:**
- Modify: `acorn/tui/menu.py` (root-level `MenuItem`s gain `value_getter` callbacks)
- Modify: `acorn/tui/settings_screen.py` (mount `DetailStrip`, wire to `Highlighted`)
- Test: `tests/test_settings_p3_visual.py`

The root four rows currently show no trailing. Per spec they should show what's inside (Preferences contents, collection count, key count, config path).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_p3_visual.py`:

```python
import pytest
from pathlib import Path
from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_root_rows_show_trailing_summaries(built_index: Path) -> None:
    """Spec: IA › Root — every drill row shows what's inside."""
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        by_label = {it.label: it for it in lst._items}
        preferences = by_label["Preferences"]
        assert preferences.trailing_value(app), "Preferences row needs a trailing summary"
        collections = by_label["Collections"]
        assert "collection" in collections.trailing_value(app).lower()
        keybindings = by_label["Keybindings"]
        assert "key" in keybindings.trailing_value(app).lower()


@pytest.mark.asyncio
async def test_detail_strip_updates_on_cursor_move(built_index: Path) -> None:
    """Spec: Visual system › Detail strip — populates on focus change."""
    from acorn.tui.settings_screen import SettingsList, SettingsScreen
    from acorn.tui.widgets import DetailStrip

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        strip = screen.query_one(DetailStrip)
        # Cursor at index 0 (Preferences). Strip shows Preferences description.
        assert "Preferences" in strip._description or "preferences" in strip._description.lower()
        # Move cursor to Collections.
        lst = screen.query_one(SettingsList)
        lst.action_move(1)
        await pilot.pause()
        assert "Collections" in strip._description or "collection" in strip._description.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_root_rows_show_trailing_summaries tests/test_settings_p3_visual.py::test_detail_strip_updates_on_cursor_move -v`
Expected: both FAIL — root rows have no `value_getter`; no `DetailStrip` mounted.

- [ ] **Step 3: Add `value_getter` callbacks to root items in `menu.py`**

In `acorn/tui/menu.py`, find `_provider_root` and replace it with:

```python
def _summary_preferences(_app: "AcornApp") -> str:
    return "Result limit · Debounce · Highlights · Defaults"


def _summary_collections(app: "AcornApp") -> str:
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is None:
        return ""
    n_collections = len(cfg.collections)
    n_sources = sum(len(c.sources) for c in cfg.collections.values())
    return f"{n_collections} collection{'s' if n_collections != 1 else ''} · {n_sources} source{'s' if n_sources != 1 else ''}"


def _summary_keybindings(app: "AcornApp") -> str:
    keymap = app._acorn_keymap  # type: ignore[attr-defined]
    n_keys = len(keymap.bindings)
    return f"{n_keys} keys across 6 contexts"


def _summary_config_path(_app: "AcornApp") -> str:
    from acorn.config import default_config_path

    p = str(default_config_path())
    # Truncate from the left for display: keep the file name visible.
    return ("…" + p[-50:]) if len(p) > 50 else p


def _provider_root(_app: "AcornApp") -> tuple[MenuItem, ...]:
    """Root settings menu — short list of categories with informative
    trailing summaries that double as the drill cue."""
    return (
        MenuItem(
            id=f"root.{SECTION_PREFERENCES}",
            label="Preferences",
            description="Adjust result limit, debounce, defaults, highlights, ranking.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_PREFERENCES),
            value_getter=_summary_preferences,
        ),
        MenuItem(
            id=f"root.{SECTION_COLLECTIONS}",
            label="Collections",
            description="Add, edit, or delete collections and their sources.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_COLLECTIONS),
            value_getter=_summary_collections,
        ),
        MenuItem(
            id=f"root.{SECTION_KEYBINDINGS}",
            label="Keybindings",
            description="Every key and what it does. Press a key in the list to invoke it.",
            kind=KIND_EXTERNAL,
            external=_open_section(SECTION_KEYBINDINGS),
            value_getter=_summary_keybindings,
        ),
        MenuItem(
            id="root.open_config_file",
            label="Open config file in editor",
            description="Drop into $EDITOR on config.toml; reload on save. Shift+Enter reveals in Finder.",
            kind=KIND_EXTERNAL,
            external=_open_config_file_action,
            value_getter=_summary_config_path,
            keywords=("edit", "config", "toml"),
        ),
    )
```

Also extend `MenuItem.trailing_value` (still in `menu.py`) so that `KIND_EXTERNAL` rows call `value_getter` too:

```python
def trailing_value(self, app: "AcornApp") -> str:
    try:
        if self.value_getter is not None:
            return self.value_getter(app)
        if self.kind == KIND_TOGGLE and self.toggle_getter is not None:
            return "On" if self.toggle_getter(app) else "Off"
        if self.kind == KIND_PICKER and self.picker_getter is not None:
            v = self.picker_getter(app)
            if isinstance(v, list):
                return f"{len(v)} selected" if v else "(none)"
            return str(v) if v not in (None, "") else "(unset)"
    except Exception:
        return ""
    return ""
```

- [ ] **Step 4: Mount `DetailStrip` on `SettingsScreen`**

In `acorn/tui/settings_screen.py`:

1. Import: `from acorn.tui.widgets import DetailStrip`.
2. In `SettingsScreen.compose`, replace `yield Static("", id="settings_status")` with `yield DetailStrip()`.
3. Wire it to `SettingsList.Highlighted`:

```python
@on(SettingsList.Highlighted)
def _on_item_highlighted(self, ev: SettingsList.Highlighted) -> None:
    strip = self.query_one(DetailStrip)
    item = ev.item
    if item is None:
        strip.clear()
        return
    metadata = self._row_metadata(item)
    strip.set(item.description or "", metadata)

def _row_metadata(self, item: MenuItem) -> str:
    """Build the 2nd-line metadata for the detail strip — storage path,
    constraint, applicability note, etc."""
    parts: list[str] = []
    if item.setting_path:
        parts.append(f"Stored in {item.setting_path}")
    if item.hint:
        parts.append(item.hint)
    if item.action_id:
        parts.append(f"Runs {item.action_id}")
    return " · ".join(parts)
```

Also delete the old `_render_status` method and its call (replaced by the strip).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_root_rows_show_trailing_summaries tests/test_settings_p3_visual.py::test_detail_strip_updates_on_cursor_move -v`
Expected: both PASS.

- [ ] **Step 6: Run the full settings test set for regression**

Run: `uv run pytest tests/test_settings_menu_p2.py tests/test_actions_keymap.py tests/test_settings_p3_visual.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add acorn/tui/menu.py acorn/tui/settings_screen.py tests/test_settings_p3_visual.py
git commit -m "feat(settings): root trailing summaries + DetailStrip (Phase 1 · Task 6 · spec §IA › Root)"
```

---

### Phase 1 verification gate

Stop. Run the full Phase 1 verification before moving on:

- [ ] Spec coverage check: every Phase 1 item from `docs/specs/2026-05-11-settings-menu-redesign.md` § Visual system, § IA › Root, § Locked decisions #6, #9 is implemented.
- [ ] Manual: `uv run acorn tui` → `:` → confirm:
  - Box is ~8 rows tall, centered, max ~100 chars wide.
  - Each of the four rows shows a dim trailing summary.
  - Detail strip below the list shows the focused row's description + metadata.
  - Cursor row shows accent-tinted background.
  - Press `?` → Keybindings opens; keys render as `[/]`, `[Space]`, `[Ctrl+C]` in accent.
  - Press `F3` → no effect.
- [ ] All tests pass: `uv run pytest tests/test_settings_p3_visual.py tests/test_settings_menu_p2.py tests/test_actions_keymap.py tests/test_phase_5_6_polish.py -v`
- [ ] Lint clean: `uv run ruff check acorn/ tests/`
- [ ] **Post a "Phase 1 done vs spec" diff and stop for explicit user sign-off.**

---

## Phase 2 — Cross-section search

Typing anywhere filters every leaf in the menu, with breadcrumbs on each result. Activation happens in-place on the current screen.

### Task 7 — `walk_all_sections` walker

**Files:**
- Modify: `acorn/tui/menu.py`
- Test: `tests/test_settings_p3_search.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_p3_search.py`:

```python
"""Phase 3 (Settings UX redesign) — cross-section search tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_walk_all_sections_includes_every_leaf(built_index: Path) -> None:
    """Spec: Search behaviour › Index — walker covers Preferences,
    Collections, Keybindings, and root-level actions."""
    from acorn.tui.menu import KIND_HEADER, walk_all_sections

    app = AcornApp(index_dir=built_index)
    async with app.run_test():
        all_items = list(walk_all_sections(app))
        labels = {item.label for _path, item in all_items}
        # Preferences leaves:
        assert "Result limit" in labels
        assert "Default collection" in labels
        # Collections section includes the per-collection drill row.
        assert "default" in labels
        # Keybindings keys (sample):
        assert any(item.label == "Quit" for _, item in all_items)
        # Root action:
        assert "Open config file in editor" in labels
        # No headers leak through.
        assert not any(item.kind == KIND_HEADER for _, item in all_items)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_search.py::test_walk_all_sections_includes_every_leaf -v`
Expected: FAIL — `walk_all_sections` doesn't exist yet.

- [ ] **Step 3: Add the walker to `menu.py`**

At the bottom of `acorn/tui/menu.py`:

```python
def walk_all_sections(app: "AcornApp") -> Iterator[tuple[tuple[str, ...], MenuItem]]:
    """Yield (breadcrumb, leaf) pairs for every selectable item across
    every section. The basis for cross-section search.

    Headers are skipped. Per-collection sub-screens are NOT descended —
    finding a collection in search drills into its editor anyway.
    """
    for section_id, label in _SECTION_LABELS.items():
        breadcrumb = (label,)
        for item in section_items(app, section_id):
            if item.kind == KIND_HEADER:
                continue
            yield breadcrumb, item
    # Root-level actions that aren't behind a category drill.
    for item in build_root_items(app):
        if item.id == "root.open_config_file":
            yield (), item
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_search.py::test_walk_all_sections_includes_every_leaf -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/menu.py tests/test_settings_p3_search.py
git commit -m "feat(menu): walk_all_sections cross-section walker (Phase 2 · Task 7 · spec §Search › Index)"
```

---

### Task 8 — Scope pseudo-row in search results

**Files:**
- Modify: `acorn/tui/menu.py`
- Test: `tests/test_settings_p3_search.py`

Searching for "scope" / "active" / "toggle collection" should surface a pseudo-row that points users to the sidebar.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_search.py`:

```python
@pytest.mark.asyncio
async def test_walk_includes_scope_pseudo_row(built_index: Path) -> None:
    """Spec: Use cases › D — pre-empt confusion about active scope by
    surfacing a sidebar pointer in cross-section results."""
    from acorn.tui.menu import walk_all_sections

    app = AcornApp(index_dir=built_index)
    async with app.run_test():
        all_items = list(walk_all_sections(app))
        scope = next(
            (item for _, item in all_items if item.id == "pseudo.scope"),
            None,
        )
        assert scope is not None
        assert "sidebar" in scope.description.lower()
        # Keywords cover the obvious search terms.
        keywords = " ".join(scope.keywords).lower()
        assert "scope" in keywords
        assert "active" in keywords
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_search.py::test_walk_includes_scope_pseudo_row -v`
Expected: FAIL — no pseudo-row.

- [ ] **Step 3: Add the pseudo-row**

In `acorn/tui/menu.py`, before the `walk_all_sections` function, add:

```python
def _pseudo_scope_row() -> MenuItem:
    """A search-only row that explains where the active-collection scope
    lives (sidebar in the main app, not the settings menu)."""
    return MenuItem(
        id="pseudo.scope",
        label="Active collection scope",
        description=(
            "Toggle which collections / sources are included in the "
            "current search scope from the main app's Collections sidebar "
            "(press `c` to focus it). Not a config setting."
        ),
        kind=KIND_ACTION,
        action_id="focus_collections_panel",
        keywords=("scope", "active", "toggle collection", "sidebar"),
    )
```

Then in `walk_all_sections`, after the `Open config file` yield, add:

```python
    # Pseudo-rows surface confusions in search without taking up real estate
    # in any sub-screen.
    yield (), _pseudo_scope_row()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_search.py::test_walk_includes_scope_pseudo_row -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/menu.py tests/test_settings_p3_search.py
git commit -m "feat(menu): scope pseudo-row in cross-section walker (Phase 2 · Task 8 · spec §Use cases › D)"
```

---

### Task 9 — Cross-section search on every settings screen

**Files:**
- Modify: `acorn/tui/settings_screen.py` (replace `_filter_items` with cross-section version; add a wrapper `MenuMatch` dataclass for the breadcrumb)
- Test: `tests/test_settings_p3_search.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_search.py`:

```python
@pytest.mark.asyncio
async def test_search_on_root_finds_preferences_leaf(built_index: Path) -> None:
    """Spec: Search behaviour — typing on root surfaces leaves from
    every section, with the breadcrumb on each row."""
    from textual.widgets import Input

    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "result limit"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        # The first item should be the Preferences › Result limit leaf.
        first = lst._items[0]
        assert first.label == "Result limit"


@pytest.mark.asyncio
async def test_search_on_keybindings_finds_preference(built_index: Path) -> None:
    """Spec: Cross-section search is global — searching from a sub-screen
    finds items in other sections."""
    from textual.widgets import Input

    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "debounce"
        await pilot.pause()
        lst = screen.query_one(SettingsList)
        labels = [it.label for it in lst._items]
        assert any("Debounce" in label for label in labels)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_settings_p3_search.py::test_search_on_root_finds_preferences_leaf tests/test_settings_p3_search.py::test_search_on_keybindings_finds_preference -v`
Expected: both FAIL — current search is per-screen.

- [ ] **Step 3: Switch `_filter_items` to use `walk_all_sections`**

In `acorn/tui/settings_screen.py`:

1. Import the walker: `from acorn.tui.menu import ..., walk_all_sections`.
2. Replace `_filter_items` body:

```python
def _filter_items(self, q: str) -> list[MenuItem]:
    """Cross-section: walk every section's leaves, score by substring
    match against label + key + keywords + breadcrumb segments."""
    from acorn.tui.menu import walk_all_sections

    matches: list[tuple[int, MenuItem, tuple[str, ...]]] = []
    for path, item in walk_all_sections(self.app):  # type: ignore[arg-type]
        if item.kind == KIND_HEADER:
            continue
        haystack = " ".join(
            (item.label, item.key, *item.keywords, *path)
        ).lower()
        idx = haystack.find(q)
        if idx == -1:
            continue
        # Earlier match in the label scores higher (sorts smaller idx first).
        label_idx = item.label.lower().find(q)
        score = label_idx if label_idx != -1 else 1000 + idx
        matches.append((score, item, path))
    matches.sort(key=lambda m: (m[0], len(m[1].label)))
    # Mutate items in-place to attach breadcrumbs as a transient attribute
    # the renderer can read; since MenuItem is frozen, store breadcrumb
    # via a side dict.
    self._search_breadcrumbs = {id(item): path for _, item, path in matches}
    return [item for _, item, _ in matches]
```

3. Update `_render_options` (the rendering loop) so it reads `self._search_breadcrumbs` for each filtered row and appends the breadcrumb in italic-dim to the right side. Look for the existing render loop and add:

```python
# After computing `label` for the row:
if self._filter_active:
    bc = self._search_breadcrumbs.get(id(item))
    if bc:
        breadcrumb_text = " › ".join(bc)
        # Right-align breadcrumb in dim italic.
        label.append("   ")
        label.append(breadcrumb_text, style="dim italic")
```

4. Initialize `self._search_breadcrumbs: dict[int, tuple[str, ...]] = {}` in `SettingsScreen.__init__`.

5. Clear breadcrumbs when the search clears:

```python
@on(Input.Changed, "#settings_search")
def _on_search_changed(self, ev: Input.Changed) -> None:
    q = ev.value.strip().lower()
    lst = self.query_one(SettingsList)
    if not q:
        self._filter_active = False
        self._search_breadcrumbs = {}
        lst.set_items(list(self._items))
        return
    self._filter_active = True
    filtered = self._filter_items(q)
    lst.set_items(filtered)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_settings_p3_search.py -v`
Expected: all PASS.

- [ ] **Step 5: Run wider regression**

Run: `uv run pytest tests/test_settings_p3_visual.py tests/test_settings_menu_p2.py tests/test_actions_keymap.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_search.py
git commit -m "feat(settings): cross-section search with breadcrumbs (Phase 2 · Task 9 · spec §Search behaviour)"
```

---

### Task 10 — Inline activation of cross-section results

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`_activate_item` already dispatches by kind; verify scalars work when the item's parent screen isn't the current one)
- Test: `tests/test_settings_p3_search.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_search.py`:

```python
@pytest.mark.asyncio
async def test_search_match_for_scalar_opens_edit_bar_inline(built_index: Path) -> None:
    """Spec: Cross-section search › Activation rule — scalar matches
    open the edit bar on the *current* screen."""
    from textual.widgets import Input

    from acorn.tui.settings_screen import (
        EditBar,
        SettingsList,
        SettingsScreen,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "result limit"
        await pilot.pause()
        # Activate the first match (which is Preferences › Result limit).
        lst = screen.query_one(SettingsList)
        lst.cursor_index = 0
        await pilot.press("enter")
        await pilot.pause()
        # We should still be on the root screen.
        assert app.screen is screen
        # The edit bar should be open with the current value populated.
        bar = screen.query_one(EditBar)
        assert "-hidden" not in bar.classes
```

- [ ] **Step 2: Run test to verify it fails OR passes**

Run: `uv run pytest tests/test_settings_p3_search.py::test_search_match_for_scalar_opens_edit_bar_inline -v`

If it fails: continue to Step 3.
If it already passes (current `_activate_item` may already do the right thing because EditBar is screen-mounted): note this in the commit message and skip to Step 5.

- [ ] **Step 3: Adjust `_activate_item` if needed**

If `_activate_item` doesn't dispatch correctly for scalars when the cursor is on a cross-section match, walk through it: scalar items have `kind == KIND_SCALAR`, so the existing branch should hit:

```python
if item.kind == KIND_SCALAR:
    current = ""
    if item.value_getter is not None:
        current = item.value_getter(app)
    self.query_one(EditBar).open(item, current)
    return
```

This should work as-is. The test failure (if any) would be from setup or from `EditCommitted` not knowing the right setting_path. Confirm by:
- Edge case: `EditCommitted` handler reads `ev.item.setting_path`. Since the menu item carries its own `setting_path`, this still works correctly even when the screen the user is on isn't Preferences.

If extra wiring is needed, add it here.

- [ ] **Step 4: Re-run the test**

Run: `uv run pytest tests/test_settings_p3_search.py::test_search_match_for_scalar_opens_edit_bar_inline -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_search.py
git commit -m "feat(settings): in-place activation of cross-section search matches (Phase 2 · Task 10 · spec §Search › Activation)"
```

---

### Phase 2 verification gate

- [ ] Spec coverage: § Search behaviour fully implemented, including "Cross-section search is global" subsection.
- [ ] Manual: `uv run acorn tui` → `:` → type `result` → see flat results with breadcrumbs across Preferences. Enter on first match → edit bar opens with `200` populated. Save → trailing value updates back on the root screen after Esc clears the search.
- [ ] Manual: `?` → type `o ` (with the trailing space) → see only Results-pane keys whose key glyph contains `o`. Pressing Enter on `[o] Open at locator` runs the action and closes the menu.
- [ ] Manual: `:` → type `scope` → scope pseudo-row appears with sidebar pointer.
- [ ] Tests: `uv run pytest tests/test_settings_p3_search.py tests/test_settings_p3_visual.py -v` all green.
- [ ] **Post Phase 2 done-vs-spec diff. Stop for user sign-off.**

---

## Phase 3 — Add Collection wizard

Single-screen wizard. Name + path + multi-select Includes + preset multi-select Excludes + DSL filter + sample tester. Live path validation. Ctrl+S saves + reindexes.

### Task 11 — Excludes presets constant

**Files:**
- Modify: `acorn/config.py`
- Test: `tests/test_settings_p3_wizard.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_p3_wizard.py`:

```python
"""Phase 3 (Settings UX redesign) — Add Collection wizard tests."""

from __future__ import annotations


def test_excludes_presets_exposed() -> None:
    """Spec: Wizard › Excludes — preset patterns, with safe defaults."""
    from acorn.config import EXCLUDES_PRESETS

    assert "hidden" in EXCLUDES_PRESETS
    hidden = EXCLUDES_PRESETS["hidden"]
    assert hidden["label"] == "Hidden / system"
    assert any(".git" in g for g in hidden["globs"])
    assert hidden["default"] is True  # pre-ticked
    assert "node_modules" in EXCLUDES_PRESETS
    assert EXCLUDES_PRESETS["node_modules"]["default"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_excludes_presets_exposed -v`
Expected: FAIL — constant missing.

- [ ] **Step 3: Add `EXCLUDES_PRESETS`**

In `acorn/config.py`, after `INDEXER_FILETYPES`:

```python
EXCLUDES_PRESETS: dict[str, dict] = {
    "hidden": {
        "label": "Hidden / system",
        "globs": ["**/.*", "**/.DS_Store", "**/.git/**"],
        "default": True,
    },
    "node_modules": {
        "label": "Node modules",
        "globs": ["**/node_modules/**"],
        "default": False,
    },
    "python_caches": {
        "label": "Python caches",
        "globs": ["**/__pycache__/**", "**/*.pyc"],
        "default": False,
    },
    "build_artefacts": {
        "label": "Build artefacts",
        "globs": ["**/dist/**", "**/build/**"],
        "default": False,
    },
    "obsidian_meta": {
        "label": "Obsidian metadata",
        "globs": ["**/.obsidian/**"],
        "default": False,
    },
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_excludes_presets_exposed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/config.py tests/test_settings_p3_wizard.py
git commit -m "feat(config): EXCLUDES_PRESETS for wizard (Phase 3 · Task 11 · spec §Wizard › Excludes)"
```

---

### Task 12 — `AddCollectionWizard` screen scaffolding

**Files:**
- Modify: `acorn/tui/settings_screen.py` (new `AddCollectionWizard` class; deprecate `NewCollectionScreen`)
- Modify: `acorn/tui/menu.py` (`_make_add_collection` returns the wizard, not the old screen)
- Test: `tests/test_settings_p3_wizard.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_wizard.py`:

```python
import pytest
from pathlib import Path
from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_add_collection_pushes_wizard_with_expected_fields(built_index: Path) -> None:
    """Spec: Wizard › Single screen — Name, Source path, Includes,
    Excludes, Frontmatter filter, Follow symlinks, plus the sample tester."""
    from acorn.tui.menu import SECTION_COLLECTIONS
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        SettingsList,
        SettingsScreen,
        open_settings_section,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        open_settings_section(app, SECTION_COLLECTIONS)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        add_idx = next(
            i for i, it in enumerate(lst._items) if it.id == "collections.add"
        )
        lst.cursor_index = add_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, AddCollectionWizard)
        # All six field rows present.
        wlst = app.screen.query_one(SettingsList)
        labels = [it.label for it in wlst._items]
        for required in ("Name", "Source path", "Includes", "Excludes",
                         "Frontmatter filter", "Follow symlinks"):
            assert required in labels, f"missing field {required!r}; got {labels}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_add_collection_pushes_wizard_with_expected_fields -v`
Expected: FAIL — `AddCollectionWizard` doesn't exist.

- [ ] **Step 3: Create the wizard class**

In `acorn/tui/settings_screen.py`, add this class near `SourceFormScreen` (so they share patterns):

```python
class AddCollectionWizard(Screen[None]):
    """Single-screen form for creating a new collection + its first source.

    Field rows live in a SettingsList; the frontmatter sample tester docks
    below. Ctrl+S validates everything and writes via write_collection +
    triggers a reindex.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape,left", "back", "Cancel", show=False),
        Binding("ctrl+s", "save_close", "Save", show=False),
        Binding("tab", "cycle_focus(1)", show=False),
        Binding("shift+tab", "cycle_focus(-1)", show=False),
    ]

    CSS = """
    AddCollectionWizard { background: $surface; align: center middle; }
    AddCollectionWizard > #settings_box {
        height: auto;
        max-height: 90%;
        width: auto;
        min-width: 72;
        max-width: 100;
        border: round $primary 50%;
        padding: 0 1;
    }
    AddCollectionWizard > #settings_box:focus-within { border: round $accent; }
    AddCollectionWizard #frontmatter_sample {
        height: 6; border: round $primary 50%; padding: 0 1;
    }
    AddCollectionWizard #frontmatter_sample:focus { border: round $accent; }
    AddCollectionWizard .form_separator { color: $text-muted; padding: 1 0 0 0; }
    AddCollectionWizard #match_status { color: $text-muted; }
    AddCollectionWizard #match_status.-match { color: $success; }
    AddCollectionWizard #match_status.-no-match { color: $error; }
    AddCollectionWizard > #footer_hints {
        dock: bottom; height: 1; background: $surface; padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._fields: dict[str, Any] = {
            "name": "",
            "path": "",
            "includes": list(),       # list of extensions (md, pdf, …) + custom globs
            "excludes_presets": list(),  # list of preset ids
            "excludes_custom": "",
            "filter": "",
            "follow_symlinks": False,
        }

    def compose(self) -> ComposeResult:
        from acorn.tui.widgets import DetailStrip

        with Vertical(id="settings_box") as box:
            box.border_title = "Add Collection"
            yield SettingsList()
            yield Static(
                "─── Test filter against sample frontmatter ───",
                classes="form_separator",
            )
            yield TextArea("", id="frontmatter_sample")
            yield Static("(no sample)", id="match_status")
            yield DetailStrip()
        yield EditBar()
        yield Static("", id="footer_hints")

    def on_mount(self) -> None:
        self._populate_fields()
        self.query_one(SettingsList).focus()
        app: AcornApp = self.app  # type: ignore[assignment]
        self.query_one("#footer_hints", Static).update(
            _hint_bar(
                app,
                (
                    ("⏎", "Edit"),
                    ("Tab", "Sample"),
                    ("Ctrl+S", "Save & Index"),
                    ("Esc", "Cancel"),
                ),
            )
        )

    def _populate_fields(self) -> None:
        self.query_one(SettingsList).set_items(self._build_field_items())

    def _build_field_items(self) -> list[MenuItem]:
        # Built out further in Task 13 (includes/excludes) and Task 14
        # (path validation). For Phase 3 Task 12 we only need the row
        # structure to exist.
        return [
            MenuItem(
                id="wiz.name",
                label="Name",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["name"] or "(required)",
            ),
            MenuItem(
                id="wiz.path",
                label="Source path",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["path"] or "(required)",
            ),
            MenuItem(
                id="wiz.includes",
                label="Includes",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._summarize_includes(),
            ),
            MenuItem(
                id="wiz.excludes",
                label="Excludes",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._summarize_excludes(),
            ),
            MenuItem(
                id="wiz.filter",
                label="Frontmatter filter",
                kind=KIND_SCALAR,
                value_getter=lambda _app: self._fields["filter"] or "(none)",
            ),
            MenuItem(
                id="wiz.follow_symlinks",
                label="Follow symlinks",
                kind=KIND_TOGGLE,
                toggle_getter=lambda _app: bool(self._fields["follow_symlinks"]),
                toggle_setter=lambda _app, v: self._set_follow(v),
            ),
        ]

    def _summarize_includes(self) -> str:
        return f"{len(self._fields['includes'])} types"

    def _summarize_excludes(self) -> str:
        return f"{len(self._fields['excludes_presets'])} presets"

    def _set_follow(self, value: bool) -> None:
        self._fields["follow_symlinks"] = bool(value)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_save_close(self) -> None:
        # Filled in Task 15.
        self.app.pop_screen()

    def action_cycle_focus(self, direction: int) -> None:
        widgets = [
            self.query_one(SettingsList),
            self.query_one("#frontmatter_sample", TextArea),
        ]
        focused = self.focused
        idx = 0
        for i, w in enumerate(widgets):
            if focused is w or (focused is not None and focused in w.walk_children()):
                idx = i
                break
        widgets[(idx + direction) % len(widgets)].focus()
```

Then in `acorn/tui/menu.py`, update `_make_add_collection`:

```python
def _make_add_collection() -> Callable[["AcornApp"], None]:
    def _open(app: "AcornApp") -> None:
        from acorn.tui.settings_screen import AddCollectionWizard

        app.push_screen(AddCollectionWizard())

    return _open
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_add_collection_pushes_wizard_with_expected_fields -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py acorn/tui/menu.py tests/test_settings_p3_wizard.py
git commit -m "feat(settings): AddCollectionWizard scaffolding (Phase 3 · Task 12 · spec §Add Collection wizard)"
```

---

### Task 13 — Includes / Excludes multi-select pickers

**Files:**
- Modify: `acorn/tui/settings_screen.py` (wire field activation to launch pickers; convert Includes / Excludes rows to KIND_PICKER)
- Test: `tests/test_settings_p3_wizard.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_wizard.py`:

```python
@pytest.mark.asyncio
async def test_includes_field_opens_filetypes_picker(built_index: Path) -> None:
    """Spec: Wizard › Includes — multi-select of indexer-supported types."""
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        PickerScreen,
        SettingsList,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        # Move cursor to the Includes row.
        inc_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.includes")
        lst.cursor_index = inc_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        # The picker shows the indexer-supported types.
        from acorn.config import INDEXER_FILETYPES

        choice_values = [c.value for c in app.screen._choices]
        assert set(choice_values) == set(INDEXER_FILETYPES.keys())


@pytest.mark.asyncio
async def test_excludes_field_opens_presets_picker_with_defaults(built_index: Path) -> None:
    """Spec: Wizard › Excludes — preset multi-select, hidden pre-checked."""
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        PickerScreen,
        SettingsList,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        exc_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.excludes")
        lst.cursor_index = exc_idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        # `hidden` preset is pre-selected.
        assert "hidden" in app.screen._selected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_includes_field_opens_filetypes_picker tests/test_settings_p3_wizard.py::test_excludes_field_opens_presets_picker_with_defaults -v`
Expected: both FAIL.

- [ ] **Step 3: Convert Includes / Excludes rows to KIND_PICKER**

In `acorn/tui/settings_screen.py`, replace `_build_field_items` rows for `wiz.includes` and `wiz.excludes`:

```python
from acorn.config import EXCLUDES_PRESETS, INDEXER_FILETYPES
from acorn.tui.menu import ChoiceOption

# In _build_field_items:
MenuItem(
    id="wiz.includes",
    label="Includes",
    kind=KIND_PICKER,
    multi=True,
    choices_provider=lambda _app: [
        ChoiceOption(value=ext, label=label)
        for ext, label in INDEXER_FILETYPES.items()
    ],
    picker_getter=lambda _app: list(self._fields["includes"]),
    picker_setter=lambda _app, vs: self._set_includes(vs),
),
MenuItem(
    id="wiz.excludes",
    label="Excludes",
    kind=KIND_PICKER,
    multi=True,
    choices_provider=lambda _app: [
        ChoiceOption(
            value=key,
            label=preset["label"],
            description=", ".join(preset["globs"]),
        )
        for key, preset in EXCLUDES_PRESETS.items()
    ],
    picker_getter=lambda _app: list(self._fields["excludes_presets"]),
    picker_setter=lambda _app, vs: self._set_excludes_presets(vs),
),
```

Then add setter methods to `AddCollectionWizard`:

```python
def _set_includes(self, values: list[str]) -> None:
    self._fields["includes"] = list(values)
    self.query_one(SettingsList).refresh_values()

def _set_excludes_presets(self, values: list[str]) -> None:
    self._fields["excludes_presets"] = list(values)
    self.query_one(SettingsList).refresh_values()
```

And initialise excludes with defaults pre-checked. In `__init__`, replace:

```python
self._fields = {
    "name": "",
    "path": "",
    "includes": [],
    "excludes_presets": [
        key for key, preset in EXCLUDES_PRESETS.items() if preset["default"]
    ],
    "excludes_custom": "",
    "filter": "",
    "follow_symlinks": False,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_includes_field_opens_filetypes_picker tests/test_settings_p3_wizard.py::test_excludes_field_opens_presets_picker_with_defaults -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_wizard.py
git commit -m "feat(settings): wizard multi-select for Includes/Excludes (Phase 3 · Task 13 · spec §Wizard › Includes/Excludes)"
```

---

### Task 14 — Live path validation in Source path edit

**Files:**
- Modify: `acorn/tui/settings_screen.py` (EditBar's path-row variant; show ✓ N files / ✗ does not exist as the user types)
- Test: `tests/test_settings_p3_wizard.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_wizard.py`:

```python
@pytest.mark.asyncio
async def test_path_validation_inline(tmp_path: Path, built_index: Path) -> None:
    """Spec: Wizard › Source path — live ✓/✗ inline validation."""
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        EditBar,
        SettingsList,
    )

    real_dir = tmp_path / "exists"
    real_dir.mkdir()
    (real_dir / "a.md").write_text("hello")

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        assert isinstance(wiz, AddCollectionWizard)
        lst = wiz.query_one(SettingsList)
        path_idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.path")
        lst.cursor_index = path_idx
        await pilot.press("enter")
        await pilot.pause()
        bar = wiz.query_one(EditBar)
        # Type a path that does not exist.
        bar.query_one("#editor_input").value = str(tmp_path / "nope")
        await pilot.pause()
        err = bar.query_one(".-edit-error").renderable
        assert "does not exist" in str(err).lower()
        # Type a path that does exist.
        bar.query_one("#editor_input").value = str(real_dir)
        await pilot.pause()
        err = bar.query_one(".-edit-error").renderable
        assert "✓" in str(err) or "1 file" in str(err).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_path_validation_inline -v`
Expected: FAIL — no live validation yet.

- [ ] **Step 3: Add path validation to `EditBar`**

In `acorn/tui/settings_screen.py`'s `EditBar`:

```python
def on_input_changed(self, ev: Input.Changed) -> None:
    """For path-typed scalars, validate on every keystroke and show
    ✓/✗ status in the error label (repurposed)."""
    if self._item is None:
        return
    if self._item.id != "wiz.path":
        return
    from pathlib import Path

    raw = ev.value.strip().strip("'\"")
    if not raw:
        self.query_one(".-edit-error", Static).update("")
        return
    p = Path(raw).expanduser()
    label = self.query_one(".-edit-error", Static)
    if not p.exists():
        label.update("[$error]✗ does not exist[/]")
        return
    if not p.is_dir():
        label.update("[$warning]⚠ not a directory[/]")
        return
    # Quick file count via scandir (capped at 5_000 for responsiveness).
    try:
        n = sum(1 for _ in p.iterdir())
    except Exception:
        label.update("[$warning]⚠ unreadable[/]")
        return
    label.update(f"[$success]✓ {n} entries[/]")
```

Note: Textual's `Input.Changed` is posted by the Input widget; the EditBar's `Horizontal` should pick it up by message routing. If not, wire via `@on(Input.Changed, "#editor_input")` decorator.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_path_validation_inline -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_wizard.py
git commit -m "feat(settings): live path validation in wizard (Phase 3 · Task 14 · spec §Wizard › Source path)"
```

---

### Task 15 — Save + reindex chain on Ctrl+S

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`AddCollectionWizard.action_save_close`)
- Test: `tests/test_settings_p3_wizard.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_wizard.py`:

```python
@pytest.mark.asyncio
async def test_save_writes_collection_and_reindexes(tmp_path, built_index: Path) -> None:
    """Spec: Wizard › Save — write_collection + reindex + drop on per-collection sub-screen."""
    from acorn.config import EXCLUDES_PRESETS, default_config_path, load
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        SettingsList,
        SettingsScreen,
    )

    real_dir = tmp_path / "vault"
    real_dir.mkdir()
    (real_dir / "a.md").write_text("# hello")

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        wiz._fields["name"] = "research"
        wiz._fields["path"] = str(real_dir)
        wiz._fields["includes"] = ["md"]
        wiz._fields["excludes_presets"] = ["hidden"]
        app.push_screen(wiz)
        await pilot.pause()
        # Trigger save.
        await pilot.press("ctrl+s")
        await pilot.pause()
        # We should land on the new collection's per-collection sub-screen.
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen._breadcrumb == ("Collections", "research")
        # The on-disk config has the new collection with the right shape.
        cfg = load(default_config_path())
        assert "research" in cfg.collections
        src = cfg.collections["research"].sources[0]
        assert str(src.path) == str(real_dir)
        # Excludes from the `hidden` preset are present.
        assert any(".git" in g for g in src.excludes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_save_writes_collection_and_reindexes -v`
Expected: FAIL — `action_save_close` is a stub.

- [ ] **Step 3: Implement `action_save_close`**

In `AddCollectionWizard`:

```python
def action_save_close(self) -> None:
    from pathlib import Path
    from acorn.config import (
        CollectionConfig,
        EXCLUDES_PRESETS,
        INDEXER_FILETYPES,
        SourceConfig,
        default_config_path,
        load,
        write_collection,
    )

    name = self._fields["name"].strip()
    path = self._fields["path"].strip().strip("'\"")
    if not name:
        self.notify("Name is required", severity="error")
        return
    if not path:
        self.notify("Source path is required", severity="error")
        return
    p = Path(path).expanduser()
    if not p.exists():
        self.notify(f"Path does not exist: {p}", severity="error")
        return

    includes_globs: list[str] = []
    for ext in self._fields["includes"]:
        # Map ext -> glob: md -> **/*.md, etc.
        includes_globs.append(f"**/*.{ext}")

    excludes_globs: list[str] = []
    for preset_id in self._fields["excludes_presets"]:
        excludes_globs.extend(EXCLUDES_PRESETS[preset_id]["globs"])
    if self._fields["excludes_custom"]:
        for g in str(self._fields["excludes_custom"]).split(","):
            g = g.strip()
            if g:
                excludes_globs.append(g)

    app: AcornApp = self.app  # type: ignore[assignment]
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is not None and name in cfg.collections:
        self.notify(f"Collection {name!r} already exists", severity="warning")
        return

    source = SourceConfig(
        path=p,
        includes=includes_globs,
        excludes=excludes_globs,
        follow_symlinks=bool(self._fields["follow_symlinks"]),
        frontmatter_filter=(self._fields["filter"] or None),
    )
    new_collection = CollectionConfig(sources=[source])
    write_collection(
        config_path=default_config_path(),
        name=name,
        collection=new_collection,
    )
    app._config = load()  # type: ignore[attr-defined]
    app._refresh_collections_panel()  # type: ignore[attr-defined]
    app._reindex_collection_async(name)  # type: ignore[attr-defined]
    # Drop wizard, then push the new collection's per-collection sub-screen.
    self.app.pop_screen()
    from acorn.tui.menu import _make_open_collection_screen

    _make_open_collection_screen(name)(app)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_save_writes_collection_and_reindexes -v`
Expected: PASS.

- [ ] **Step 5: Wider regression**

Run: `uv run pytest tests/ --tb=short -q 2>&1 | tail -20`
Expected: all green; if existing collection tests reference the old `NewCollectionScreen`, update them to use `AddCollectionWizard`.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_wizard.py
git commit -m "feat(settings): wizard save + reindex chain (Phase 3 · Task 15 · spec §Wizard › Save)"
```

---

### Task 16 — Esc cancels with zero side effects

**Files:**
- Test: `tests/test_settings_p3_wizard.py`

`AddCollectionWizard.action_back` already just pops the screen — verify it leaves no orphan empty collections.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_wizard.py`:

```python
@pytest.mark.asyncio
async def test_esc_discards_wizard_with_no_side_effects(built_index: Path) -> None:
    """Spec: Wizard › Esc — cancelling after typing a name does NOT
    create an empty collection."""
    from acorn.config import default_config_path, load
    from acorn.tui.settings_screen import AddCollectionWizard

    before = load(default_config_path()).collections.copy()

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        wiz._fields["name"] = "ghost"
        app.push_screen(wiz)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    after = load(default_config_path()).collections
    assert "ghost" not in after, "Esc must not create an empty collection"
    assert set(after.keys()) == set(before.keys())
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_esc_discards_wizard_with_no_side_effects -v`
Expected: PASS already (wizard `action_back` is just `pop_screen`).

If it fails (because some side effect leaked), trace and fix.

- [ ] **Step 3: Commit**

```bash
git add tests/test_settings_p3_wizard.py
git commit -m "test(settings): verify wizard Esc is side-effect-free (Phase 3 · Task 16 · spec §Wizard › Esc)"
```

---

### Phase 3 verification gate

- [ ] Spec coverage: § Add Collection wizard fully implemented (name, path, includes picker, excludes preset picker, filter, follow symlinks, sample tester, save+reindex chain, Esc-safe).
- [ ] Manual: `:` → Collections → Add collection → fill name "research", path `~/Documents`, tick three file types, leave `hidden` ticked → Ctrl+S → land on `Settings › Collections › research`, reindex notification appears.
- [ ] Manual: open wizard, type a name, Esc → no orphan collection in `config.toml`.
- [ ] Manual: Source path with `~/nope/nonexistent` → trailing in edit bar shows `✗ does not exist`.
- [ ] Tests: `uv run pytest tests/test_settings_p3_wizard.py -v` all green.
- [ ] **Post Phase 3 done-vs-spec diff. Stop for user sign-off.**

---

## Phase 4 — Reveal-in-Finder + Open keybindings file

Two related affordances: `Shift+Enter` reveals the file backing a reveal-capable row; a new "Open keybindings file in editor" sibling action lives on the root.

### Task 17 — `acorn.opener.reveal(path)` helper

**Files:**
- Modify: `acorn/opener.py`
- Test: `tests/test_settings_p3_reveal.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_p3_reveal.py`:

```python
"""Phase 3 (Settings UX redesign) — reveal & open-keybindings tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch


def test_reveal_runs_open_R_on_macos(tmp_path: Path) -> None:
    """Spec: Reveal-in-Finder — uses `open -R <path>` on macOS."""
    from acorn import opener

    p = tmp_path / "x.toml"
    p.write_text("")
    with patch.object(subprocess, "Popen") as mock_popen:
        opener.reveal(p)
        mock_popen.assert_called_once()
        args = mock_popen.call_args.args[0]
        assert args[0] == "open"
        assert args[1] == "-R"
        assert args[2] == str(p)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_reveal.py::test_reveal_runs_open_R_on_macos -v`
Expected: FAIL — `opener.reveal` doesn't exist.

- [ ] **Step 3: Add `reveal()` to `acorn/opener.py`**

At the end of `acorn/opener.py`:

```python
def reveal(path: Path | str) -> None:
    """Reveal ``path`` in Finder (selected) via macOS `open -R`.

    Fire-and-forget — uses Popen so the TUI doesn't block on Finder's
    launch latency. On non-macOS platforms this is a no-op for now (the
    project targets macOS per pyproject).
    """
    import platform
    import subprocess

    if platform.system() != "Darwin":
        return
    subprocess.Popen(
        ["open", "-R", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_reveal.py::test_reveal_runs_open_R_on_macos -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/opener.py tests/test_settings_p3_reveal.py
git commit -m "feat(opener): reveal() helper for Shift+Enter reveal-in-Finder (Phase 4 · Task 17 · spec §Reveal pattern)"
```

---

### Task 18 — `Shift+Enter` binding on reveal-capable rows

**Files:**
- Modify: `acorn/tui/settings_screen.py` (binding on `SettingsList`; `action_reveal` calls `opener.reveal()` for rows with a reveal-capable id)
- Test: `tests/test_settings_p3_reveal.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
import pytest
from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir, tmp_index_dir):
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_shift_enter_on_open_config_calls_reveal(built_index) -> None:
    """Spec: Reveal pattern — Shift+Enter on the Open config row reveals
    config.toml in Finder."""
    from unittest.mock import patch
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        idx = next(
            i for i, it in enumerate(lst._items) if it.id == "root.open_config_file"
        )
        lst.cursor_index = idx
        with patch("acorn.opener.reveal") as mock_reveal:
            await pilot.press("shift+enter")
            await pilot.pause()
            mock_reveal.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_reveal.py::test_shift_enter_on_open_config_calls_reveal -v`
Expected: FAIL.

- [ ] **Step 3: Add the binding and action**

In `SettingsList.BINDINGS`, add:

```python
Binding("shift+enter", "reveal", show=False),
```

Then add a method on `SettingsList`:

```python
def action_reveal(self) -> None:
    """Shift+Enter on a reveal-capable row triggers Finder reveal of
    the underlying file. Capability is keyed off well-known row ids."""
    if not (0 <= self.cursor_index < len(self._items)):
        return
    item = self._items[self.cursor_index]
    path = self._reveal_target(item)
    if path is None:
        return
    from acorn import opener
    opener.reveal(path)

def _reveal_target(self, item: MenuItem) -> "Path | None":
    """Return the file path to reveal for ``item``, or None if the row
    isn't reveal-capable."""
    from pathlib import Path
    from acorn.config import default_config_path

    if item.id == "root.open_config_file":
        return default_config_path()
    if item.id == "root.open_keybindings_file":
        return Path(default_config_path()).parent / "keybindings.toml"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_reveal.py::test_shift_enter_on_open_config_calls_reveal -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_reveal.py
git commit -m "feat(settings): Shift+Enter reveal binding (Phase 4 · Task 18 · spec §Reveal pattern)"
```

---

### Task 19 — `Open keybindings file in editor` root action

**Files:**
- Modify: `acorn/tui/menu.py` (add a fifth root row)
- Modify: `acorn/tui/app.py` (add `action_open_keybindings_file`)
- Test: `tests/test_settings_p3_reveal.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_root_has_open_keybindings_file(built_index) -> None:
    """Spec: IA › Root — sibling action for the keybindings TOML."""
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        lst = app.screen.query_one(SettingsList)
        labels = [it.label for it in lst._items]
        assert "Open keybindings file in editor" in labels
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_reveal.py::test_root_has_open_keybindings_file -v`
Expected: FAIL.

- [ ] **Step 3: Add the row and action**

In `acorn/tui/menu.py`, in `_provider_root` after the `Open config file` MenuItem:

```python
MenuItem(
    id="root.open_keybindings_file",
    label="Open keybindings file in editor",
    description="Drop into $EDITOR on keybindings.toml; Shift+Enter reveals in Finder.",
    kind=KIND_EXTERNAL,
    external=lambda app: app.action_open_keybindings_file(),
    value_getter=_summary_keybindings_path,
    keywords=("edit", "keybindings", "rebind"),
),
```

Add the summary helper:

```python
def _summary_keybindings_path(_app: "AcornApp") -> str:
    from acorn.config import default_config_path

    p = str(default_config_path().parent / "keybindings.toml")
    return ("…" + p[-50:]) if len(p) > 50 else p
```

Then in `acorn/tui/app.py`, add `action_open_keybindings_file`. Pattern follows `action_open_config_file`:

```python
def action_open_keybindings_file(self) -> None:
    """Drop into $EDITOR on keybindings.toml; reload keymap on save."""
    import os
    import subprocess
    from acorn.config import default_config_path

    path = default_config_path().parent / "keybindings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Acorn user keybinding overrides.\n# [normal]\n# \"j\"    = \"focus_results_pane\"\n", encoding="utf-8")
    # Pop the settings stack so the editor takes over cleanly.
    from acorn.tui.settings_screen import SettingsScreen
    while isinstance(self.screen, SettingsScreen):
        self.pop_screen()
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with self.suspend():
        subprocess.call([editor, str(path)])
    # Reload the keymap.
    from acorn.tui.actions import load_keymap
    self._acorn_keymap = load_keymap()
    self.notify("Reloaded keybindings", timeout=2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_reveal.py::test_root_has_open_keybindings_file -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/menu.py acorn/tui/app.py tests/test_settings_p3_reveal.py
git commit -m "feat(settings): Open keybindings file action (Phase 4 · Task 19 · spec §IA › Open keybindings file)"
```

---

### Phase 4 verification gate

- [ ] Spec coverage: § Reveal-in-Finder pattern + § Open keybindings file row.
- [ ] Manual: `:` → cursor on `Open config file` → bottom hint bar shows `Shift+⏎ Reveal` (depends on Task 6 hint-bar work — verify). Press Shift+Enter → Finder opens with config.toml selected.
- [ ] Manual: `:` → cursor on `Open keybindings file in editor` → Enter → `$EDITOR` opens keybindings.toml. Quit editor → notify "Reloaded keybindings".
- [ ] Tests: `uv run pytest tests/test_settings_p3_reveal.py -v` all green.
- [ ] **Post Phase 4 done-vs-spec diff. Stop for user sign-off.**

---

## Phase 5 — Press-key-to-invoke + drill-cue user preference

Lazygit-style "press the listed key to run the action" on the Keybindings screen; the user-controllable drill summary mode.

### Task 20 — Press-key-to-invoke on Keybindings

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`SettingsScreen.on_key` override for Keybindings sub-screen)
- Test: `tests/test_settings_p3_keybindings_invoke.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_settings_p3_keybindings_invoke.py`:

```python
"""Phase 3 — press-key-to-invoke on Keybindings + drill cue mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from acorn.index import build_index
from acorn.tui import AcornApp


@pytest.fixture
def built_index(fixtures_dir: Path, tmp_index_dir: Path) -> Path:
    build_index(roots=[fixtures_dir], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_pressing_key_in_keybindings_invokes_action(built_index: Path) -> None:
    """Spec: Keybindings › Press-key-to-invoke — pressing a listed key
    dispatches the action and closes the settings stack."""
    from textual.widgets import Input
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        # Focus the list (not the search input).
        screen.query_one(SettingsList).focus()
        # Press `o` — should run action_open_at_locator and close menu.
        await pilot.press("o")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.asyncio
async def test_pressing_key_while_search_focused_does_not_invoke(built_index: Path) -> None:
    """Spec: Press-key-to-invoke applies only when the LIST has focus;
    typing in the search filter must not trigger actions."""
    from textual.widgets import Input
    from acorn.tui.settings_screen import SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one("#settings_search", Input).focus()
        await pilot.press("o")
        await pilot.pause()
        # Search has 'o' in it; menu still up.
        assert isinstance(app.screen, SettingsScreen)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_settings_p3_keybindings_invoke.py -v`
Expected: both FAIL.

- [ ] **Step 3: Override `on_key` on `SettingsScreen`**

In `acorn/tui/settings_screen.py`'s `SettingsScreen`, add:

```python
async def on_key(self, ev: events.Key) -> None:
    """Lazygit-style press-key-to-invoke on the Keybindings sub-screen.

    Only fires when the screen's breadcrumb ends in "Keybindings" AND
    focus is on the list (not the search input). Looks for a row whose
    `key` field matches the pressed key; if found, dispatches the
    action and closes the settings stack.
    """
    if self._breadcrumb[-1:] != ("Keybindings",):
        return
    focused = self.focused
    if focused is None or not isinstance(focused, SettingsList):
        return
    pressed = ev.key
    # Normalise: Textual's `space` -> "Space", etc.
    pressed_label = _normalise_key_label(pressed)
    for item in self.query_one(SettingsList)._items:
        if item.kind == KIND_HEADER or not item.key:
            continue
        if item.key.lower() == pressed_label.lower():
            ev.stop()
            ev.prevent_default()
            # Close settings, dispatch.
            self._close_settings_stack()
            if item.action_id:
                method = getattr(self.app, f"action_{item.action_id}", None)
                if callable(method):
                    method()
            return


def _normalise_key_label(key: str) -> str:
    """Map Textual's key names to the labels used in MenuItem.key."""
    return {
        "space": "Space",
        "ctrl+c": "Ctrl+C",
        "shift+enter": "Shift+Enter",
        "tab": "Tab",
        "question_mark": "?",
        "colon": ":",
        "slash": "/",
    }.get(key, key)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_settings_p3_keybindings_invoke.py -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_keybindings_invoke.py
git commit -m "feat(settings): press-key-to-invoke on Keybindings (Phase 5 · Task 20 · spec §Keybindings)"
```

---

### Task 21 — `Defaults.drill_summary_mode` config field

**Files:**
- Modify: `acorn/config.py` (`Defaults` model)
- Modify: `acorn/config.py` (`CONFIG_TEMPLATE` — document the new field)
- Test: `tests/test_settings_p3_keybindings_invoke.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_drill_summary_mode_default_and_validation() -> None:
    """Spec: Drill-cue preference — defaults to always_show; validates set."""
    from acorn.config import Defaults
    from pydantic import ValidationError

    d = Defaults()
    assert d.drill_summary_mode == "always_show"
    # Each known mode round-trips.
    for mode in ("always_show", "smart", "always_ellipsis"):
        Defaults(drill_summary_mode=mode)
    # Unknown values rejected.
    try:
        Defaults(drill_summary_mode="banana")
    except ValidationError:
        return
    raise AssertionError("expected ValidationError for unknown mode")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_keybindings_invoke.py::test_drill_summary_mode_default_and_validation -v`
Expected: FAIL.

- [ ] **Step 3: Extend `Defaults`**

In `acorn/config.py`:

```python
from typing import Literal


class Defaults(BaseModel):
    collection: str = "default"
    result_limit: int = 200
    preview_chunks: int = 5
    debounce_ms: int = 200
    drill_summary_mode: Literal["always_show", "smart", "always_ellipsis"] = "always_show"
```

Update `CONFIG_TEMPLATE` to mention it:

```
debounce_ms   = 200           # Wait this many ms after the last keystroke (0-2000).
# How drill-in row trailing summaries render in the Settings menu:
#   always_show       (default): each row shows its content summary
#   smart                       : summary only on rows with real content
#   always_ellipsis             : a dim `…` on every drill row
drill_summary_mode = "always_show"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_keybindings_invoke.py::test_drill_summary_mode_default_and_validation -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/config.py tests/test_settings_p3_keybindings_invoke.py
git commit -m "feat(config): drill_summary_mode field (Phase 5 · Task 21 · spec §Drill-cue preference)"
```

---

### Task 22 — Wire `drill_summary_mode` into row rendering

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`_render_row` reads the mode and adjusts)
- Modify: `acorn/tui/menu.py` (add the `drill_summary_mode` picker to Preferences › Display)
- Test: `tests/test_settings_p3_keybindings_invoke.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_drill_mode_always_ellipsis(built_index: Path, tmp_path: Path) -> None:
    """Spec: Drill-cue preference — `always_ellipsis` mode renders `…`
    instead of content summaries."""
    from acorn.config import default_config_path, load, write_setting
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    write_setting(
        config_path=default_config_path(),
        dotted_path="defaults.drill_summary_mode",
        value="always_ellipsis",
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        preferences = next(it for it in lst._items if it.label == "Preferences")
        # In always_ellipsis mode the trailing value is `…`.
        assert preferences.trailing_value(app) == "…"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_keybindings_invoke.py::test_drill_mode_always_ellipsis -v`
Expected: FAIL — `trailing_value` ignores the preference.

- [ ] **Step 3: Adjust `MenuItem.trailing_value`**

In `acorn/tui/menu.py`'s `MenuItem.trailing_value`:

```python
def trailing_value(self, app: "AcornApp") -> str:
    try:
        cfg = getattr(app, "_config", None)
        mode = (
            cfg.defaults.drill_summary_mode
            if cfg and hasattr(cfg.defaults, "drill_summary_mode")
            else "always_show"
        )
        # Drill rows obey the user's preference.
        if self.kind == KIND_EXTERNAL and self.value_getter is not None:
            if mode == "always_ellipsis":
                return "…"
            if mode == "smart":
                # Only return a summary if value_getter returns something
                # meaningful (non-empty, not the path-fallback).
                v = self.value_getter(app)
                return v if v else "…"
            return self.value_getter(app)
        # Non-drill rows: setting values / toggle states always shown.
        if self.value_getter is not None:
            return self.value_getter(app)
        if self.kind == KIND_TOGGLE and self.toggle_getter is not None:
            return "On" if self.toggle_getter(app) else "Off"
        if self.kind == KIND_PICKER and self.picker_getter is not None:
            v = self.picker_getter(app)
            if isinstance(v, list):
                return f"{len(v)} selected" if v else "(none)"
            return str(v) if v not in (None, "") else "(unset)"
    except Exception:
        return ""
    return ""
```

Then expose the mode as a picker in Preferences. In `acorn/tui/menu.py`'s `_provider_preferences`, after the `Highlights` toggle and inside the Display sub-group:

```python
MenuItem(
    id="pref.drill_summary_mode",
    label="Drill row summaries",
    description="How drill-in rows render their trailing column.",
    kind=KIND_PICKER,
    choices_provider=lambda _app: [
        ChoiceOption(value="always_show", label="Always show summary"),
        ChoiceOption(value="smart", label="Smart (only when informative)"),
        ChoiceOption(value="always_ellipsis", label="Always show … only"),
    ],
    picker_getter=lambda app: app._config.defaults.drill_summary_mode if app._config else "always_show",
    picker_setter=_setting_writer("defaults.drill_summary_mode"),
    keywords=("drill", "summary", "trailing"),
),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_keybindings_invoke.py::test_drill_mode_always_ellipsis -v`
Expected: PASS.

- [ ] **Step 5: Wider regression**

Run: `uv run pytest tests/ --tb=short -q 2>&1 | tail -10`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/menu.py acorn/tui/settings_screen.py tests/test_settings_p3_keybindings_invoke.py
git commit -m "feat(settings): wire drill_summary_mode into renderer (Phase 5 · Task 22 · spec §Drill-cue preference)"
```

---

### Phase 5 verification gate

- [ ] Spec coverage: § Keybindings press-key-to-invoke + § Drill-cue user preference.
- [ ] Manual: `?` → focus list (not search) → press `o` → main app, focused result opens at locator.
- [ ] Manual: `:` → Preferences → Display → `Drill row summaries` → pick "Always show … only" → back to root. Each drill row trailing is `…`. Switch back to "Always show summary" → summaries return.
- [ ] Tests: `uv run pytest tests/test_settings_p3_keybindings_invoke.py -v` all green.
- [ ] Tests (whole suite): `uv run pytest tests/ -v 2>&1 | tail -5` all green.
- [ ] Lint: `uv run ruff check acorn/ tests/`
- [ ] **Post Phase 5 done-vs-spec diff. Stop for user sign-off.**

---

## Phase 6 — Audit amendments

Plan-vs-spec audit (post-write self-review) surfaced nine spec items that the first 22 tasks did not cover. Each amendment below cites the spec section it implements. Tasks here run **after Phase 5 lands** so the visual / search / wizard scaffolding they sit on top of is already in place.

| # | Spec reference                                       | Gap                                                                                                | Resolution            |
|---|------------------------------------------------------|----------------------------------------------------------------------------------------------------|-----------------------|
| G1 | §IA › Collections sub-screen                        | Collection rows lack `● 3 sources · ranking:default` trailing values.                              | Task 23              |
| G2 | §IA › Sources sub-screen                            | Source rows lack file-types + `⚠ path not found` trailing summaries.                                | Task 23              |
| G3 | §Design system › Hint bar (four context variants)   | Reveal-aware / Keybindings / search-focused / edit-bar-open variants not all wired.                | Task 24              |
| G4 | §IA › Add Collection wizard (Includes + Excludes)   | `Custom glob… (text input)` escape hatch not in the picker.                                        | Task 25              |
| G5 | §IA › Per-source form                               | Existing per-source form uses free-text Includes/Excludes; spec says "same shape as the wizard".  | Task 26              |
| G6 | §Search behaviour › Match display                   | "Bold-substring of the matched query inside the label" not implemented.                            | Task 27              |
| G7 | §Search behaviour › Empty-state hint                | `No matches for '<query>'…` static is not rendered when the filter is empty.                       | Task 28              |
| G8 | Locked decision #12 (inline errors only)            | Task 15's save path uses `self.notify(…)` for validation failures.                                | Task 15 amendment (Task 29) |
| G9 | §Use cases › A4 (find version / config path)        | Root status area shows config path via the row, but no version line.                              | Task 6 amendment (Task 30)  |

---

### Task 23 — Collection-row and source-row trailing summaries

**Spec:** §IA › Collections sub-screen + §IA › Sources sub-screen

**Files:**
- Modify: `acorn/tui/menu.py` (`_provider_collections` per-collection rows + `_provider_sources` per-source rows)
- Test: `tests/test_settings_p3_visual.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_visual.py`:

```python
@pytest.mark.asyncio
async def test_collection_row_shows_source_count_and_ranking(built_index: Path) -> None:
    """Spec: IA › Collections sub-screen — each collection row's trailing
    shows `<n> source(s) · ranking:<profile>` with scope dot prefix."""
    from acorn.tui.menu import SECTION_COLLECTIONS, section_items

    app = AcornApp(index_dir=built_index)
    async with app.run_test():
        items = section_items(app, SECTION_COLLECTIONS)
        default = next(it for it in items if it.id == "collections.default")
        trailing = default.trailing_value(app)
        assert "source" in trailing.lower()
        assert "ranking" in trailing.lower()
        # Scope dot ● or ○ is rendered at the start.
        assert trailing[0] in ("●", "○")


@pytest.mark.asyncio
async def test_source_row_shows_filetypes_and_path_warning(tmp_path: Path, built_index: Path) -> None:
    """Spec: IA › Sources sub-screen — source rows show file-types and
    `⚠ path not found` when the path no longer resolves."""
    from acorn.config import (
        CollectionConfig,
        SourceConfig,
        default_config_path,
        write_collection,
    )
    from acorn.tui.menu import section_items

    # Make a collection with two sources: one valid, one missing.
    real = tmp_path / "exists"
    real.mkdir()
    (real / "a.md").write_text("x")
    write_collection(
        config_path=default_config_path(),
        name="probe",
        collection=CollectionConfig(
            sources=[
                SourceConfig(path=real, includes=["**/*.md"]),
                SourceConfig(path=tmp_path / "nope", includes=["**/*.pdf"]),
            ]
        ),
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test():
        items = section_items(app, "collections:probe:sources")
        valid = next(it for it in items if it.id == "source.probe.0")
        missing = next(it for it in items if it.id == "source.probe.1")
        assert "md" in valid.trailing_value(app).lower()
        assert "⚠" in missing.trailing_value(app)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_collection_row_shows_source_count_and_ranking tests/test_settings_p3_visual.py::test_source_row_shows_filetypes_and_path_warning -v`
Expected: both FAIL — provider rows have no `value_getter`.

- [ ] **Step 3: Wire `value_getter` into the collection-row provider**

In `acorn/tui/menu.py`, find `_provider_collections`. For every per-collection `MenuItem` it emits, attach a `value_getter`:

```python
def _collection_trailing(name: str) -> Callable[["AcornApp"], str]:
    def _summary(app: "AcornApp") -> str:
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or name not in cfg.collections:
            return ""
        coll = cfg.collections[name]
        active = name in (app._active_collections or set())  # type: ignore[attr-defined]
        dot = "●" if active else "○"
        n = len(coll.sources)
        plural = "s" if n != 1 else ""
        profile = coll.ranking_profile or "default"
        return f"{dot} {n} source{plural} · ranking:{profile}"

    return _summary


# Inside _provider_collections, when building each per-collection row:
MenuItem(
    id=f"collections.{name}",
    label=name,
    description=f"Edit, rename, reindex, or delete the `{name}` collection.",
    kind=KIND_EXTERNAL,
    external=_make_open_collection_screen(name),
    value_getter=_collection_trailing(name),
)
```

If `app._active_collections` doesn't exist as an attribute, fall back to checking `cfg.defaults.collection == name` (single-active model). Keep the dot semantic clear.

- [ ] **Step 4: Wire `value_getter` into the source-row provider**

In `_provider_sources`, attach:

```python
def _source_trailing(collection_name: str, idx: int) -> Callable[["AcornApp"], str]:
    def _summary(app: "AcornApp") -> str:
        cfg = app._config  # type: ignore[attr-defined]
        if cfg is None or collection_name not in cfg.collections:
            return ""
        sources = cfg.collections[collection_name].sources
        if idx >= len(sources):
            return ""
        src = sources[idx]
        # Map *.md → md, *.pdf → pdf and present as a comma list.
        exts: list[str] = []
        for g in src.includes:
            for ext in INDEXER_FILETYPES:
                if g.endswith(f".{ext}"):
                    exts.append(ext)
                    break
        types = ", ".join(exts) if exts else "Custom"
        suffix = ""
        if not src.path.exists():
            suffix = " · ⚠ path not found"
        return f"{types}{suffix}"

    return _summary


# Inside _provider_sources, for each source row:
MenuItem(
    id=f"source.{collection_name}.{i}",
    label=f"{i + 1}. {src.path}",
    description=f"Edit source #{i + 1} of `{collection_name}`.",
    kind=KIND_EXTERNAL,
    external=_make_open_source_form(collection_name, i),
    value_getter=_source_trailing(collection_name, i),
)
```

Make sure `INDEXER_FILETYPES` is imported at the top of `menu.py`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_collection_row_shows_source_count_and_ranking tests/test_settings_p3_visual.py::test_source_row_shows_filetypes_and_path_warning -v`
Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/menu.py tests/test_settings_p3_visual.py
git commit -m "feat(menu): collection + source row trailing summaries (Phase 6 · Task 23 · spec §IA Collections/Sources)"
```

---

### Task 24 — Hint bar context variants

**Spec:** §Design system › Hint bar (four context variants)

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`_refresh_hint_bar` chooses variant based on focus + cursor row + breadcrumb)
- Test: `tests/test_settings_p3_visual.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_p3_visual.py`:

```python
@pytest.mark.asyncio
async def test_hint_bar_appends_reveal_when_cursor_on_reveal_capable_row(built_index: Path) -> None:
    """Spec: Hint bar — append `Shift+⏎ Reveal` when row supports reveal."""
    from textual.widgets import Static
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        lst = screen.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "root.open_config_file")
        lst.cursor_index = idx
        await pilot.pause()
        hints = str(screen.query_one("#footer_hints", Static).renderable)
        assert "Shift" in hints and "Reveal" in hints


@pytest.mark.asyncio
async def test_hint_bar_keybindings_variant(built_index: Path) -> None:
    """Spec: Hint bar — Keybindings screen shows `⏎ Run · [key] Run directly · Esc Back`."""
    from textual.widgets import Static
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_show_help()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        screen.query_one(SettingsList).focus()
        await pilot.pause()
        hints = str(screen.query_one("#footer_hints", Static).renderable)
        assert "Run" in hints
        assert "directly" in hints.lower() or "[key]" in hints
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_hint_bar_appends_reveal_when_cursor_on_reveal_capable_row tests/test_settings_p3_visual.py::test_hint_bar_keybindings_variant -v`
Expected: both FAIL.

- [ ] **Step 3: Add variant logic to `_refresh_hint_bar` (or `on_mount` if static)**

In `acorn/tui/settings_screen.py`'s `SettingsScreen`, add:

```python
def _refresh_hint_bar(self) -> None:
    """Recompute the contextual cluster based on focus, breadcrumb, and
    the cursor row. Called from `_on_item_highlighted`, `on_focus`, and
    `EditBar` open/close events."""
    from textual.widgets import Static
    app: AcornApp = self.app  # type: ignore[assignment]
    focused = self.focused

    # Edit-bar open: minimal save/cancel pair.
    bar = self.query_one(EditBar)
    if "-hidden" not in bar.classes:
        cluster = (("⏎", "Save"), ("Esc", "Cancel"))
        self.query_one("#footer_hints", Static).update(_hint_bar(app, cluster))
        return

    # Search input has focus.
    from textual.widgets import Input
    if isinstance(focused, Input) and focused.id == "settings_search":
        cluster = (("↓", "Results"), ("⏎", "Open first"), ("Esc", "Clear"))
        self.query_one("#footer_hints", Static).update(_hint_bar(app, cluster))
        return

    # Keybindings sub-screen.
    if self._breadcrumb[-1:] == ("Keybindings",):
        cluster = (("⏎", "Run"), ("[key]", "Run directly"), ("Esc", "Back"))
        self.query_one("#footer_hints", Static).update(_hint_bar(app, cluster))
        return

    # Default cluster — possibly with Shift+Enter Reveal appended.
    cluster = (("↑↓", "Nav"), ("⏎", "Open"), ("←", "Back"), ("/", "Filter"))
    lst = self.query_one(SettingsList)
    if 0 <= lst.cursor_index < len(lst._items):
        item = lst._items[lst.cursor_index]
        if item.id in ("root.open_config_file", "root.open_keybindings_file"):
            cluster = cluster + (("Shift+⏎", "Reveal"),)
    self.query_one("#footer_hints", Static).update(_hint_bar(app, cluster))
```

Then call `self._refresh_hint_bar()` from:
- `on_mount` (after the existing hint-bar write)
- `_on_item_highlighted` (after `strip.set(…)`)
- `EditBar`'s `open(…)` and `close()` (via a `screen._refresh_hint_bar()` callback)
- Any focus change on the search input (via `@on(events.Focus)` or `@on(events.Blur)`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_hint_bar_appends_reveal_when_cursor_on_reveal_capable_row tests/test_settings_p3_visual.py::test_hint_bar_keybindings_variant -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_visual.py
git commit -m "feat(settings): contextual hint bar variants (Phase 6 · Task 24 · spec §Hint bar)"
```

---

### Task 25 — Custom glob escape hatch in wizard pickers

**Spec:** §IA › Add Collection wizard — Includes ("Custom glob… (text input)") + Excludes ("Custom globs… (free text)")

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`PickerScreen` supports a `custom` row; `AddCollectionWizard._fields` already has `excludes_custom`, add `includes_custom`)
- Test: `tests/test_settings_p3_wizard.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_wizard.py`:

```python
@pytest.mark.asyncio
async def test_includes_picker_includes_custom_entry(built_index: Path) -> None:
    """Spec: Wizard › Includes — `Custom glob… (text input)` escape hatch."""
    from acorn.tui.settings_screen import (
        AddCollectionWizard,
        PickerScreen,
        SettingsList,
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(AddCollectionWizard())
        await pilot.pause()
        wiz = app.screen
        lst = wiz.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "wiz.includes")
        lst.cursor_index = idx
        await pilot.press("enter")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, PickerScreen)
        values = [c.value for c in picker._choices]
        assert "__custom__" in values, f"expected custom entry; got {values}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_includes_picker_includes_custom_entry -v`
Expected: FAIL.

- [ ] **Step 3: Add the custom entry to both pickers**

In `acorn/tui/settings_screen.py`, update `AddCollectionWizard._build_field_items` so the Includes and Excludes `choices_provider` callbacks append a final entry with `value="__custom__"`:

```python
choices_provider=lambda _app: [
    ChoiceOption(value=ext, label=label)
    for ext, label in INDEXER_FILETYPES.items()
] + [
    ChoiceOption(
        value="__custom__",
        label="Custom glob…",
        description="Add a free-form glob pattern (e.g. `**/*.org`).",
    )
],
```

When the picker commits and the selection set includes `"__custom__"`, the wizard's `_set_includes` opens the EditBar pre-populated with the existing custom value, captures the entry, and stores it in `self._fields["includes_custom"]` (initialise to `""` in `__init__`). The picker's selection set itself stores `"__custom__"` as a sentinel; the renderer maps that to `<n> + custom` for the row's trailing summary.

Do the same for `wiz.excludes` and its existing `excludes_custom` field.

In `action_save_close` (Task 15), when assembling `includes_globs`, append the custom value if set:

```python
if self._fields.get("includes_custom"):
    for g in str(self._fields["includes_custom"]).split(","):
        g = g.strip()
        if g:
            includes_globs.append(g)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_includes_picker_includes_custom_entry -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_wizard.py
git commit -m "feat(settings): custom glob escape hatch in wizard pickers (Phase 6 · Task 25 · spec §Wizard › Includes/Excludes)"
```

---

### Task 26 — Per-source form uses wizard pickers

**Spec:** §IA › Per-source form ("Same shape as the Add Collection wizard, but pre-populated")

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`SourceFormScreen`)
- Test: `tests/test_settings_p3_wizard.py` (append)

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_source_form_uses_picker_for_includes(built_index: Path, tmp_path: Path) -> None:
    """Spec: Per-source form — Includes is a multi-select picker, not free text."""
    from acorn.config import (
        CollectionConfig,
        SourceConfig,
        default_config_path,
        write_collection,
    )
    from acorn.tui.menu import _make_open_source_form
    from acorn.tui.settings_screen import (
        PickerScreen,
        SettingsList,
        SourceFormScreen,
    )

    real = tmp_path / "vault"
    real.mkdir()
    write_collection(
        config_path=default_config_path(),
        name="probe2",
        collection=CollectionConfig(
            sources=[SourceConfig(path=real, includes=["**/*.md", "**/*.pdf"])],
        ),
    )

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        _make_open_source_form("probe2", 0)(app)
        await pilot.pause()
        form = app.screen
        assert isinstance(form, SourceFormScreen)
        lst = form.query_one(SettingsList)
        idx = next(i for i, it in enumerate(lst._items) if it.id == "src.includes")
        lst.cursor_index = idx
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PickerScreen)
        # md and pdf pre-selected from the existing globs.
        assert "md" in app.screen._selected
        assert "pdf" in app.screen._selected
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_source_form_uses_picker_for_includes -v`
Expected: FAIL.

- [ ] **Step 3: Convert `SourceFormScreen` field rows to pickers**

In `SourceFormScreen._build_field_items`, replace the existing free-text Includes/Excludes rows with the same picker rows the wizard uses. Add a one-time parse from existing globs back to ext set + presets:

```python
def _split_includes(globs: list[str]) -> tuple[list[str], str]:
    """Map a list of globs to (ext set, custom blob)."""
    exts: list[str] = []
    custom: list[str] = []
    for g in globs:
        matched = False
        for ext in INDEXER_FILETYPES:
            if g == f"**/*.{ext}":
                exts.append(ext)
                matched = True
                break
        if not matched:
            custom.append(g)
    return exts, ", ".join(custom)


def _split_excludes(globs: list[str]) -> tuple[list[str], str]:
    """Map a list of excludes globs back to preset set + custom blob."""
    preset_keys: list[str] = []
    leftover: list[str] = list(globs)
    for key, preset in EXCLUDES_PRESETS.items():
        if all(g in leftover for g in preset["globs"]):
            preset_keys.append(key)
            for g in preset["globs"]:
                leftover.remove(g)
    return preset_keys, ", ".join(leftover)
```

In `SourceFormScreen.__init__`:

```python
exts, includes_custom = _split_includes(list(source.includes))
preset_keys, excludes_custom = _split_excludes(list(source.excludes))
self._fields = {
    "path": str(source.path),
    "includes": exts,
    "includes_custom": includes_custom,
    "excludes_presets": preset_keys,
    "excludes_custom": excludes_custom,
    "filter": source.frontmatter_filter or "",
    "follow_symlinks": bool(source.follow_symlinks),
}
```

Wire the field rows exactly as the wizard does — `KIND_PICKER` with `INDEXER_FILETYPES` / `EXCLUDES_PRESETS` + `__custom__` entry; setters update `self._fields` and call `refresh_values`.

On `action_save_close`, reassemble globs the same way the wizard does and write via `write_collection_source`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_source_form_uses_picker_for_includes -v`
Expected: PASS.

- [ ] **Step 5: Wider regression — existing per-source tests**

Run: `uv run pytest tests/test_collections_screen.py tests/test_settings_menu_p2.py -v`
Expected: PASS — existing tests that assert on field labels still hold; assertions on free-text inputs need updates if any.

- [ ] **Step 6: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_wizard.py
git commit -m "feat(settings): per-source form uses wizard pickers (Phase 6 · Task 26 · spec §Per-source form)"
```

---

### Task 27 — Match-substring bolding in search results

**Spec:** §Search behaviour › Match display ("Bold-substring of the matched query inside the label")

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`_render_row` accepts an optional `highlight: str` param and bolds the substring)
- Test: `tests/test_settings_p3_search.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_search.py`:

```python
def test_search_result_label_has_bold_substring_for_query() -> None:
    """Spec: Search › Match display — matched substring is bold inside label."""
    from acorn.tui.menu import KIND_SCALAR, MenuItem
    from acorn.tui.settings_screen import _render_row

    item = MenuItem(id="x", label="Result limit", kind=KIND_SCALAR)
    rendered = _render_row(item, app=None, width=80, highlight="result")
    # Walk Rich Text spans and confirm at least one segment over the
    # "Result" substring carries a bold style.
    spans = rendered.spans
    label_str = str(rendered)
    assert "Result" in label_str
    bold_segments = [
        s for s in spans
        if "bold" in str(s.style).lower()
    ]
    assert bold_segments, "expected bold span over matched substring"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_search.py::test_search_result_label_has_bold_substring_for_query -v`
Expected: FAIL.

- [ ] **Step 3: Add `highlight` parameter to `_render_row`**

In `acorn/tui/settings_screen.py`:

```python
def _render_row(
    item: MenuItem,
    app: AcornApp | None,
    width: int | None = None,
    highlight: str | None = None,
) -> Text:
    """… (existing docstring)

    If ``highlight`` is given and appears (case-insensitive) inside the
    label, the matching substring is rendered bold to surface why the row
    was returned by search.
    """
    # … existing key + label assembly …
    # Replace the plain `text.append(item.label)` with:
    if highlight:
        low = item.label.lower()
        h_low = highlight.lower()
        i = low.find(h_low)
        if i >= 0:
            text.append(item.label[:i])
            text.append(item.label[i : i + len(highlight)], style="bold")
            text.append(item.label[i + len(highlight) :])
        else:
            text.append(item.label)
    else:
        text.append(item.label)
```

Then update `SettingsList`'s render loop (in `set_items` / `_render_options`) to pass the current search query when the filter is active:

```python
highlight = self._search_query if self._filter_active else None
rendered = _render_row(item, self.app, width=self.size.width or 80, highlight=highlight)
```

`SettingsScreen` stores the current search query on the `SettingsList` (the search input handler already does this) — surface it as `lst._search_query: str = ""` and set it in `_on_search_changed`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_search.py::test_search_result_label_has_bold_substring_for_query -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_search.py
git commit -m "feat(settings): bold matched substring in search labels (Phase 6 · Task 27 · spec §Search › Match display)"
```

---

### Task 28 — Empty-state hint for zero-match search

**Spec:** §Search behaviour › Empty-state hint

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`SettingsList` renders a single placeholder row when filtered list is empty)
- Test: `tests/test_settings_p3_search.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_search.py`:

```python
@pytest.mark.asyncio
async def test_zero_match_shows_empty_state_hint(built_index: Path) -> None:
    """Spec: Search › Empty-state hint — `No matches for '<q>'` placeholder."""
    from textual.widgets import Input
    from acorn.tui.settings_screen import SettingsList, SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        search = screen.query_one("#settings_search", Input)
        search.value = "zzzzzzz-no-match"
        await pilot.pause()
        # The list should render an empty-state row.
        lst = screen.query_one(SettingsList)
        labels = [it.label for it in lst._items]
        assert any("No matches" in label for label in labels), labels
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_search.py::test_zero_match_shows_empty_state_hint -v`
Expected: FAIL.

- [ ] **Step 3: Synthesise a placeholder row on empty filter**

In `_on_search_changed` (or wherever the filter applies):

```python
@on(Input.Changed, "#settings_search")
def _on_search_changed(self, ev: Input.Changed) -> None:
    q = ev.value.strip().lower()
    lst = self.query_one(SettingsList)
    lst._search_query = q
    if not q:
        self._filter_active = False
        self._search_breadcrumbs = {}
        lst.set_items(list(self._items))
        return
    self._filter_active = True
    filtered = self._filter_items(q)
    if not filtered:
        from acorn.tui.menu import KIND_HEADER
        empty = MenuItem(
            id="search.empty",
            label=f"No matches for '{ev.value.strip()}'. Try shorter terms or press Esc to clear.",
            kind=KIND_HEADER,  # non-selectable
        )
        lst.set_items([empty])
        return
    lst.set_items(filtered)
```

Use `KIND_HEADER` so the cursor-skip rule keeps the row inert.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_search.py::test_zero_match_shows_empty_state_hint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_search.py
git commit -m "feat(settings): empty-state hint for zero-match search (Phase 6 · Task 28 · spec §Empty-state hint)"
```

---

### Task 29 — Replace `notify()` with inline errors in wizard

**Spec:** Locked decision #12 — "Inline errors only — no toast notifications for in-form failures."

**Files:**
- Modify: `acorn/tui/settings_screen.py` (`AddCollectionWizard.action_save_close`; add `#wizard_error` Static below the form)
- Test: `tests/test_settings_p3_wizard.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_settings_p3_wizard.py`:

```python
@pytest.mark.asyncio
async def test_save_with_missing_name_shows_inline_error(built_index: Path) -> None:
    """Spec: Locked decision #12 — inline error, no toast."""
    from textual.widgets import Static
    from acorn.tui.settings_screen import AddCollectionWizard

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        wiz = AddCollectionWizard()
        # Path set, name blank.
        wiz._fields["path"] = "/tmp"
        app.push_screen(wiz)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        # We should still be on the wizard.
        assert isinstance(app.screen, AddCollectionWizard)
        err = app.screen.query_one("#wizard_error", Static)
        assert "name" in str(err.renderable).lower()
        assert "required" in str(err.renderable).lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_save_with_missing_name_shows_inline_error -v`
Expected: FAIL — `#wizard_error` doesn't exist; current code uses `self.notify`.

- [ ] **Step 3: Add `#wizard_error` Static, route validation to it**

In `AddCollectionWizard.compose`, mount the inline error widget below the sample tester:

```python
yield Static("", id="wizard_error", classes="-hidden")
```

CSS additions:

```css
AddCollectionWizard #wizard_error { height: auto; color: $error; padding: 0 1; }
AddCollectionWizard #wizard_error.-hidden { display: none; }
```

Rewrite `action_save_close` to write to the inline error instead of notifying:

```python
def _show_error(self, message: str) -> None:
    err = self.query_one("#wizard_error", Static)
    err.update(message)
    err.remove_class("-hidden")

def _clear_error(self) -> None:
    err = self.query_one("#wizard_error", Static)
    err.update("")
    err.add_class("-hidden")

def action_save_close(self) -> None:
    # … existing imports …
    self._clear_error()

    name = self._fields["name"].strip()
    path = self._fields["path"].strip().strip("'\"")
    if not name:
        self._show_error("Name is required.")
        return
    if not path:
        self._show_error("Source path is required.")
        return
    p = Path(path).expanduser()
    if not p.exists():
        self._show_error(f"Path does not exist: {p}")
        return
    # … existing duplicate-name check, also via _show_error …
    app: AcornApp = self.app  # type: ignore[assignment]
    cfg = app._config  # type: ignore[attr-defined]
    if cfg is not None and name in cfg.collections:
        self._show_error(f"Collection '{name}' already exists.")
        return
    # … remainder unchanged (write, reindex, drop on per-collection sub-screen) …
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_wizard.py::test_save_with_missing_name_shows_inline_error -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py tests/test_settings_p3_wizard.py
git commit -m "fix(settings): inline wizard errors instead of notify (Phase 6 · Task 29 · spec §Locked decisions #12)"
```

---

### Task 30 — Version line on the root status row

**Spec:** §Use cases › A4 ("find version / config path" — at the bottom of the root screen)

**Files:**
- Modify: `acorn/tui/settings_screen.py` (root-only status line below the DetailStrip showing `acorn vX.Y.Z`)
- Test: `tests/test_settings_p3_visual.py` (append)

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_root_screen_shows_version_status_line(built_index: Path) -> None:
    """Spec: Use case A4 — version visible at bottom of root menu."""
    from textual.widgets import Static
    from acorn.tui.settings_screen import SettingsScreen

    app = AcornApp(index_dir=built_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        status = screen.query_one("#settings_status", Static)
        from acorn import __version__
        assert __version__ in str(status.renderable)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_root_screen_shows_version_status_line -v`
Expected: FAIL — Task 6 removed the status widget in favour of DetailStrip; this restores a narrower one (root-only).

- [ ] **Step 3: Mount `#settings_status` on root only**

In `SettingsScreen.compose`, after the DetailStrip:

```python
if not self._breadcrumb:  # root only
    yield Static("", id="settings_status")
```

In `on_mount`, when on root:

```python
if not self._breadcrumb:
    from acorn import __version__
    self.query_one("#settings_status", Static).update(
        f"acorn v{__version__}"
    )
```

If `acorn.__version__` is missing, expose it via `acorn/__init__.py` (read from `importlib.metadata.version("acorn")` with a fallback constant).

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_settings_p3_visual.py::test_root_screen_shows_version_status_line -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add acorn/tui/settings_screen.py acorn/__init__.py tests/test_settings_p3_visual.py
git commit -m "feat(settings): version status line on root menu (Phase 6 · Task 30 · spec §Use case A4)"
```

---

### Phase 6 verification gate

- [ ] All nine gaps from the audit table at the top of this section have a passing test.
- [ ] Manual: `:` → cursor on `Open config file` → footer ends with `Shift+⏎ Reveal`. Press `?` → footer changes to `⏎ Run · [key] Run directly · Esc Back`.
- [ ] Manual: `:` → Collections → see rows with `● 3 sources · ranking:default` trailings.
- [ ] Manual: Open a per-source form → Includes opens a multi-select picker pre-checked for `md`, `pdf` etc.
- [ ] Manual: `:` → type `result` → first hit shows `**Result** limit` (bold "Result" substring).
- [ ] Manual: `:` → type `zzzzz` → list shows the placeholder hint.
- [ ] Manual: `:` → Add collection → Ctrl+S with no name → inline red error appears below the form (no toast).
- [ ] Manual: `:` → root shows `acorn vX.Y.Z` at the bottom.
- [ ] Tests: `uv run pytest tests/test_settings_p3_visual.py tests/test_settings_p3_search.py tests/test_settings_p3_wizard.py tests/test_settings_p3_reveal.py tests/test_settings_p3_keybindings_invoke.py -v`
- [ ] Lint: `uv run ruff check acorn/ tests/`
- [ ] **Post Phase 6 done-vs-spec diff. Stop for user sign-off.**

---

## Final sign-off

Before marking the redesign complete, complete the full spec verification list from `docs/specs/2026-05-11-settings-menu-redesign.md` § Verification.

```bash
uv run pytest tests/ -v
uv run ruff check acorn/ tests/
```

Manual walkthrough (every numbered item in the spec's Verification section). For each, paste the observed behaviour into a "done-vs-spec" comment on the PR/branch so the diff is auditable.
