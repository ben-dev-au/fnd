# Phase 5.5e-3 — TUI Collections Form Implementation Plan


**Spec:** [`docs/specs/2026-05-09-collection-crud-and-source-filters-design.md`](../specs/2026-05-09-collection-crud-and-source-filters-design.md) — section "TUI (`fnd/tui/collections_screen.py`)".

**Goal:** Add a full-screen Collections form behind `F3` / `:collections` so users can list, edit, save, and delete collections without leaving the TUI. The form is a thin lens over the user's `config.toml`; saves round-trip via `tomlkit` so hand-authored comments survive.

**Architecture:** A new `fnd/tui/collections_screen.py` module exports `CollectionsScreen(Screen)` — a Textual `Screen` subclass pushed onto the app stack. It composes a left-pane collections list, a right-pane editor showing the selected collection's sources, and pushes a `SourceEditScreen(ModalScreen)` for per-source edits. Save uses a new `fnd/config.py:write_collection` helper (full-collection round-trip via `tomlkit`, complementing the existing `write_collection_source`). Reindex is invoked synchronously after save with a small status-line message; auto-fired only when path / includes / excludes / `frontmatter_filter` changed for any source.

**Tech Stack:** Python 3.13, Textual 0.85+ (Screen, ModalScreen, Input, TextArea, Button, Static), tomlkit, pytest, pytest-asyncio (existing).

---

## Scope vs. defer

| In | Out (5.5e-3.x polish) |
|---|---|
| F3 / `:collections` action | Browse-for-path file picker |
| Collections list + editor pane | Per-collection ranking-profile dropdown (use `config edit`) |
| Source-edit modal (path / includes / excludes / filter) | Drag-reorder of sources |
| Filter parse-status indicator | `fnd collection rm` CLI (form covers it) |
| "Test against pasted frontmatter" affordance | Multi-collection bulk operations |
| Save round-trip via tomlkit (preserves comments) | |
| Delete collection (with confirmation) | |
| New collection (inline name prompt → empty collection) | |
| Auto-reindex on structural change | |

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `fnd/tui/collections_screen.py` | create | `CollectionsScreen`, `SourceEditScreen`, helper widgets |
| `fnd/tui/actions.py` | modify | Add `open_collections_form` action |
| `fnd/tui/app.py` | modify | Implement `action_open_collections_form`; pass `Config` mutation hook back to the main app |
| `fnd/config.py` | modify | Add `write_collection(*, config_path, collection_name, collection)` (full round-trip); add `delete_collection(*, config_path, name)` |
| `tests/test_collections_screen.py` | create | Pilot-based behaviour tests |
| `tests/test_config_write_collection.py` | create | tomlkit round-trip preserves comments + adjacent tables |

---

## Conventions

- All Python: `from __future__ import annotations` at top.
- Tests: `pytest.mark.asyncio` with `app.run_test()` + `pilot` for TUI behaviour. Read `app._collections_screen`, etc. for state.
- One commit per task; Conventional Commits with §5.5e-3 reference.
- Pre-commit (ruff + pyright strict + pytest-fast) on every commit. Don't bypass.

---

## Task 1: Action + F3 binding + screen skeleton

**Files:**
- Modify: `fnd/tui/actions.py`
- Create: `fnd/tui/collections_screen.py`
- Modify: `fnd/tui/app.py` (`action_open_collections_form`)
- Test: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Create `tests/test_collections_screen.py`:

```python
"""Phase 5.5e-3: TUI Collections form — F3 / :collections."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from textual.widgets import Static

from fnd.config import Config, load
from fnd.tui import FNDApp


@pytest.fixture
def cfg_with_one_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/notes"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    monkeypatch.setattr("fnd.cli.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.mark.asyncio
async def test_f3_opens_collections_screen(
    cfg_with_one_collection: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_with_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # The screen mounts a Static with the title "Collections".
        title = app.query_one("#collections_title", Static)
        assert "collections" in str(title.renderable).lower()


@pytest.mark.asyncio
async def test_escape_closes_collections_screen(
    cfg_with_one_collection: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_with_one_collection)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        assert app.query("#collections_title")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.query("#collections_title")
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 2 failures — F3 doesn't open anything yet.

- [ ] **Step 3: Add the action**

Append to the `REGISTRY` tuple in `fnd/tui/actions.py`:

```python
    Action(
        id="open_collections_form",
        description="Open the Collections form (add / edit / delete collections).",
        default_key="f3",
        command="collections",
        footer_label="Collections",
    ),
```

- [ ] **Step 4: Create the screen skeleton**

Create `fnd/tui/collections_screen.py`:

```python
"""TUI Collections form (§5.5e-3).

Full-screen Textual ``Screen`` that lists configured collections and lets
the user edit, add, or delete them without leaving the TUI. Saves persist
to ``config.toml`` via :func:`fnd.config.write_collection`, which uses
``tomlkit`` so user-authored comments survive the round-trip.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static

from fnd.config import Config


class CollectionsScreen(Screen[None]):
    """Top-level Collections form. Pushed from the main app via F3."""

    BINDINGS = [  # noqa: RUF012 — Textual class-list pattern
        Binding("escape", "close", "Close", show=True),
    ]

    CSS = """
    CollectionsScreen { background: $surface; }
    #collections_title { dock: top; height: 1; padding: 0 1; background: $panel; color: $accent; text-style: bold; }
    #collections_body { width: 100%; height: 1fr; }
    #collections_list_pane { width: 1fr; height: 1fr; border: round $primary; padding: 1; }
    #collections_editor_pane { width: 2fr; height: 1fr; border: round $primary; padding: 1; }
    """

    def __init__(self, config: Config, *, config_path: Path) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        yield Static("Collections", id="collections_title")
        with Horizontal(id="collections_body"):
            yield Vertical(id="collections_list_pane")
            yield Vertical(id="collections_editor_pane")
        yield Footer()

    def action_close(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 5: Wire `action_open_collections_form` in `fnd/tui/app.py`**

Find the existing action methods (the block of `action_*` methods on `FNDApp`). Add:

```python
    def action_open_collections_form(self) -> None:
        """Push the Collections screen for browsing / editing collections."""
        from fnd.config import default_config_path

        from fnd.tui.collections_screen import CollectionsScreen

        # Use the config that was loaded at TUI launch as the starting point;
        # the screen will reload from disk before showing to pick up any
        # external edits.
        if self._config is None:
            return
        screen = CollectionsScreen(self._config, config_path=default_config_path())
        self.push_screen(screen, callback=self._on_collections_form_dismissed)

    def _on_collections_form_dismissed(self, _result: object) -> None:
        """The form may have written changes to disk; reload our cached
        Config so subsequent searches use the new collection set."""
        from fnd.config import load

        self._config = load()
        # Recompute ranking profile in case the active collection's profile
        # was edited.
        self._ranking_profile = self._resolve_profile()
        self._refresh_status()
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 2 passed.

- [ ] **Step 7: Full suite**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add fnd/tui/actions.py fnd/tui/collections_screen.py fnd/tui/app.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — F3 / :collections opens screen skeleton"
```

---

## Task 2: Collections list pane

**Files:**
- Modify: `fnd/tui/collections_screen.py`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Append to `tests/test_collections_screen.py`:

```python
@pytest.fixture
def cfg_three_collections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Config:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.papers.sources]]
            path = "/tmp/papers"

            [[collections.coursework.sources]]
            path = "/tmp/notes"
            includes = ["**/*.md"]

            [[collections.coursework.sources]]
            path = "/tmp/decks"
            includes = ["**/*.pdf"]

            [[collections.notes.sources]]
            path = "/tmp/zk"
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    return load(cfg_path)


@pytest.mark.asyncio
async def test_collections_list_shows_each_with_source_count(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # The list pane should have three rows (one per collection) showing
        # the name and source count.
        list_pane = app.query_one("#collections_list_pane")
        text = list_pane.render_str().plain  # type: ignore[attr-defined]
        # If render_str isn't exposed, walk children:
        from textual.widgets import ListView, Static

        statics = list_pane.query(Static)
        text = "\n".join(str(s.renderable) for s in statics)
        assert "papers" in text
        assert "coursework" in text
        assert "notes" in text
        assert "1 source" in text or "1 sources" in text
        assert "2 sources" in text
```

(If the test's `render_str` approach doesn't fit Textual's API for the pane you build, fall back to walking child widgets and reading their `renderable`. The assertion is "name and source count both appear in the visible content of the list pane".)

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py::test_collections_list_shows_each_with_source_count -v`
Expected: fail — list pane is empty in the skeleton.

- [ ] **Step 3: Render the list**

Modify `compose()` of `CollectionsScreen` to populate the list pane. Replace the `Vertical(id="collections_list_pane")` line with a call to a helper that fills it:

```python
    def compose(self) -> ComposeResult:
        yield Static("Collections", id="collections_title")
        with Horizontal(id="collections_body"):
            with Vertical(id="collections_list_pane"):
                yield from self._collection_rows()
            yield Vertical(id="collections_editor_pane")
        yield Footer()

    def _collection_rows(self):
        from textual.widgets import Static

        names = sorted(self._config.collections.keys())
        if not names:
            yield Static("(no collections — press n to add one)")
            return
        for name in names:
            collection = self._config.collections[name]
            count = len(collection.sources)
            label = f"{name}  ({count} source{'s' if count != 1 else ''})  [{collection.ranking_profile}]"
            yield Static(label, classes="collection_row", id=f"collection_row_{name}")
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_collections_screen.py::test_collections_list_shows_each_with_source_count -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add fnd/tui/collections_screen.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — collections list pane shows name + source count"
```

---

## Task 3: Editor pane shows selected collection's sources (read-only)

**Files:**
- Modify: `fnd/tui/collections_screen.py`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Append:

```python
@pytest.mark.asyncio
async def test_clicking_collection_shows_its_sources(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Default selection: first alphabetically (coursework). Editor pane
        # should already show its two sources without any extra interaction.
        editor = app.query_one("#collections_editor_pane")
        from textual.widgets import Static

        text = "\n".join(
            str(s.renderable) for s in editor.query(Static)
        )
        assert "/tmp/notes" in text
        assert "/tmp/decks" in text
        assert "**/*.md" in text
        assert "**/*.pdf" in text
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py::test_clicking_collection_shows_its_sources -v`
Expected: fail — editor pane is empty.

- [ ] **Step 3: Render the editor pane**

Add to `CollectionsScreen`:

```python
    def __init__(self, config: Config, *, config_path: Path) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        # Default selection: first collection alphabetically.
        self._selected: str | None = (
            sorted(config.collections.keys())[0] if config.collections else None
        )
```

Update `compose` to render the editor pane against `self._selected`:

```python
    def compose(self) -> ComposeResult:
        yield Static("Collections", id="collections_title")
        with Horizontal(id="collections_body"):
            with Vertical(id="collections_list_pane"):
                yield from self._collection_rows()
            with Vertical(id="collections_editor_pane"):
                yield from self._editor_rows()
        yield Footer()

    def _editor_rows(self):
        from textual.widgets import Static

        if self._selected is None:
            yield Static("Select a collection on the left, or press n to add a new one.")
            return
        c = self._config.collections[self._selected]
        yield Static(f"Editing: {self._selected}", classes="editor_heading")
        yield Static(f"Ranking: {c.ranking_profile}")
        yield Static("Sources:")
        if not c.sources:
            yield Static("  (none — press a to add a source)")
            return
        for i, s in enumerate(c.sources, start=1):
            yield Static(f"  {i}. {s.path}", classes="source_row")
            if s.includes:
                yield Static(f"     includes: {', '.join(s.includes)}")
            if s.excludes:
                yield Static(f"     excludes: {', '.join(s.excludes)}")
            if s.frontmatter_filter:
                yield Static(f"     filter:   {s.frontmatter_filter}")
```

- [ ] **Step 4: Add j/k navigation**

Add bindings + actions on `CollectionsScreen`:

```python
    BINDINGS = [
        Binding("escape", "close", "Close", show=True),
        Binding("j,down", "list_next", "Next", show=False),
        Binding("k,up", "list_prev", "Prev", show=False),
    ]

    def action_list_next(self) -> None:
        names = sorted(self._config.collections.keys())
        if not names or self._selected is None:
            return
        i = names.index(self._selected)
        self._selected = names[(i + 1) % len(names)]
        self._refresh()

    def action_list_prev(self) -> None:
        names = sorted(self._config.collections.keys())
        if not names or self._selected is None:
            return
        i = names.index(self._selected)
        self._selected = names[(i - 1) % len(names)]
        self._refresh()

    def _refresh(self) -> None:
        """Re-render both panes; cheap because the form is small."""
        list_pane = self.query_one("#collections_list_pane", Vertical)
        editor_pane = self.query_one("#collections_editor_pane", Vertical)
        list_pane.remove_children()
        editor_pane.remove_children()
        list_pane.mount_all(self._collection_rows())
        editor_pane.mount_all(self._editor_rows())
```

(Note: the `_collection_rows` and `_editor_rows` helpers may need to highlight the selected name. Add a `>` prefix or a CSS class on the selected row. Keep it simple for now — a unicode `▸` prefix suffices.)

Update `_collection_rows` to mark the selection:

```python
    def _collection_rows(self):
        from textual.widgets import Static

        names = sorted(self._config.collections.keys())
        if not names:
            yield Static("(no collections — press n to add one)")
            return
        for name in names:
            collection = self._config.collections[name]
            count = len(collection.sources)
            marker = "▸ " if name == self._selected else "  "
            label = (
                f"{marker}{name}  ({count} source{'s' if count != 1 else ''}) "
                f" [{collection.ranking_profile}]"
            )
            yield Static(label, classes="collection_row", id=f"collection_row_{name}")
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add fnd/tui/collections_screen.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — editor pane shows selected collection's sources"
```

---

## Task 4: Source-edit modal (text inputs + filter parse-status)

**Files:**
- Modify: `fnd/tui/collections_screen.py`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Append:

```python
@pytest.mark.asyncio
async def test_pressing_e_opens_source_edit_modal(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Default selection: coursework (alphabetical first). Press 'e'
        # to edit the first source.
        await pilot.press("e")
        await pilot.pause()
        # Modal mounts an input with id source_path_input.
        assert app.query("#source_path_input")


@pytest.mark.asyncio
async def test_invalid_filter_shows_parse_error(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        from textual.widgets import Input, Static

        filter_input = app.query_one("#source_filter_input", Input)
        filter_input.value = "Course =="  # invalid DSL
        # Filter parse-status should pick up the change after the input
        # event fires.
        await pilot.pause()
        status = app.query_one("#filter_parse_status", Static)
        assert "col" in str(status.renderable).lower() or "error" in str(status.renderable).lower()
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 2 new failures (e doesn't do anything, no #source_path_input).

- [ ] **Step 3: Implement `SourceEditScreen` (modal)**

Append to `fnd/tui/collections_screen.py`:

```python
class SourceEditScreen(Screen[dict | None]):
    """Modal for editing one source. Returns the edited fields (or None
    if cancelled) via :meth:`Screen.dismiss`. The parent
    :class:`CollectionsScreen` applies the change to its in-memory
    Config and re-renders.
    """

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "save", "Save", show=True),
    ]

    CSS = """
    SourceEditScreen { align: center middle; background: $surface 80%; }
    #source_edit_box { width: 80%; height: auto; border: round $accent; padding: 1; background: $surface; }
    #source_edit_box Input { margin-bottom: 1; }
    #filter_parse_status { color: $success; }
    .filter_parse_error { color: $error; }
    """

    def __init__(
        self,
        *,
        title: str,
        path: str,
        includes: list[str],
        excludes: list[str],
        frontmatter_filter: str | None,
    ) -> None:
        super().__init__()
        self._title = title
        self._initial = {
            "path": path,
            "includes": ",".join(includes),
            "excludes": ",".join(excludes),
            "frontmatter_filter": frontmatter_filter or "",
        }

    def compose(self) -> ComposeResult:
        from textual.widgets import Input

        with Vertical(id="source_edit_box"):
            yield Static(f"Edit source — {self._title}", classes="editor_heading")
            yield Static("Path:")
            yield Input(value=self._initial["path"], id="source_path_input")
            yield Static("Includes (comma-separated globs):")
            yield Input(value=self._initial["includes"], id="source_includes_input")
            yield Static("Excludes (comma-separated globs):")
            yield Input(value=self._initial["excludes"], id="source_excludes_input")
            yield Static("Frontmatter filter (DSL):")
            yield Input(
                value=self._initial["frontmatter_filter"], id="source_filter_input"
            )
            yield Static("✓ filter parses", id="filter_parse_status")
            yield Static("ctrl+s save · esc cancel", classes="footer_hint")

    def on_input_changed(self, ev) -> None:  # noqa: ANN001
        if ev.input.id != "source_filter_input":
            return
        from fnd.filter_dsl import parse_or_error

        text = ev.value
        status = self.query_one("#filter_parse_status", Static)
        if not text.strip():
            status.update("(no filter)")
            status.remove_class("filter_parse_error")
            return
        _pred, err = parse_or_error(text)
        if err is None:
            status.update("✓ filter parses")
            status.remove_class("filter_parse_error")
        else:
            status.update(f"✗ col {err.column}: {err.message}")
            status.add_class("filter_parse_error")

    def action_save(self) -> None:
        from textual.widgets import Input
        from fnd.filter_dsl import parse_or_error

        filter_text = self.query_one("#source_filter_input", Input).value.strip()
        if filter_text:
            _pred, err = parse_or_error(filter_text)
            if err is not None:
                # Refuse to save: surface a notify, leave the modal open.
                self.app.notify(
                    f"col {err.column}: {err.message}",
                    severity="error",
                    title="Filter syntax",
                )
                return
        result = {
            "path": self.query_one("#source_path_input", Input).value.strip(),
            "includes": [
                s.strip()
                for s in self.query_one("#source_includes_input", Input).value.split(",")
                if s.strip()
            ],
            "excludes": [
                s.strip()
                for s in self.query_one("#source_excludes_input", Input).value.split(",")
                if s.strip()
            ],
            "frontmatter_filter": filter_text or None,
        }
        self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Wire `e` to push the modal in `CollectionsScreen`**

Add to `CollectionsScreen.BINDINGS`:

```python
        Binding("e", "edit_first_source", "Edit source 1", show=False),
```

Add the action method:

```python
    def action_edit_first_source(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if not c.sources:
            return
        s = c.sources[0]
        screen = SourceEditScreen(
            title=f"{self._selected} / 1",
            path=str(s.path),
            includes=list(s.includes),
            excludes=list(s.excludes),
            frontmatter_filter=s.frontmatter_filter,
        )
        self.app.push_screen(screen, callback=lambda r: self._apply_source_edit(0, r))

    def _apply_source_edit(self, index: int, result: dict | None) -> None:
        if result is None or self._selected is None:
            return
        from fnd.config import SourceConfig

        c = self._config.collections[self._selected]
        new_source = SourceConfig(
            path=Path(result["path"]),
            includes=result["includes"],
            excludes=result["excludes"],
            frontmatter_filter=result["frontmatter_filter"],
            follow_symlinks=c.sources[index].follow_symlinks,
        )
        c.sources[index] = new_source
        self._refresh()
```

(Editing the n-th source via a list-cursor lands in Task 5 — for now `e` always edits the first source. Task 5 adds proper cursor.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add fnd/tui/collections_screen.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — source-edit modal with filter parse status"
```

---

## Task 5: Source cursor + add/remove

**Files:**
- Modify: `fnd/tui/collections_screen.py`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Append:

```python
@pytest.mark.asyncio
async def test_a_adds_blank_source_row(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # coursework starts with 2 sources.
        await pilot.press("a")
        await pilot.pause()
        # The source-edit modal should be open with empty fields.
        from textual.widgets import Input

        path_input = app.query_one("#source_path_input", Input)
        assert path_input.value == ""


@pytest.mark.asyncio
async def test_x_removes_focused_source(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # coursework / first source is the focused one. Press 'x' to remove.
        await pilot.press("x")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, type(app.query_one("#collections_title").screen))  # CollectionsScreen
        c = screen._config.collections["coursework"]  # type: ignore[attr-defined]
        assert len(c.sources) == 1  # was 2, now 1
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 2 new failures.

- [ ] **Step 3: Add a per-collection source cursor**

Update `CollectionsScreen.__init__`:

```python
        self._source_cursor: int = 0
```

Reset `_source_cursor` to 0 in `action_list_next` and `action_list_prev`.

Update `_editor_rows` to show the source cursor (highlight the focused row):

```python
        for i, s in enumerate(c.sources):
            marker = "▸ " if i == self._source_cursor else "  "
            yield Static(f"{marker}{i + 1}. {s.path}", classes="source_row")
            # ... include/exclude/filter lines ...
```

Add bindings:

```python
        Binding("J", "source_next", "Source ↓", show=False),
        Binding("K", "source_prev", "Source ↑", show=False),
        Binding("a", "add_source", "Add source", show=True),
        Binding("x", "remove_source", "Remove source", show=True),
```

Add actions:

```python
    def action_source_next(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if c.sources:
            self._source_cursor = (self._source_cursor + 1) % len(c.sources)
        self._refresh()

    def action_source_prev(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if c.sources:
            self._source_cursor = (self._source_cursor - 1) % len(c.sources)
        self._refresh()

    def action_add_source(self) -> None:
        if self._selected is None:
            return
        screen = SourceEditScreen(
            title=f"{self._selected} / new",
            path="",
            includes=[],
            excludes=[],
            frontmatter_filter=None,
        )
        self.app.push_screen(screen, callback=self._on_new_source_dismissed)

    def _on_new_source_dismissed(self, result: dict | None) -> None:
        if result is None or self._selected is None:
            return
        from fnd.config import SourceConfig

        c = self._config.collections[self._selected]
        c.sources.append(
            SourceConfig(
                path=Path(result["path"]),
                includes=result["includes"],
                excludes=result["excludes"],
                frontmatter_filter=result["frontmatter_filter"],
            )
        )
        self._source_cursor = len(c.sources) - 1
        self._refresh()

    def action_remove_source(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if not c.sources:
            return
        del c.sources[self._source_cursor]
        if c.sources:
            self._source_cursor = min(self._source_cursor, len(c.sources) - 1)
        else:
            self._source_cursor = 0
        self._refresh()
```

Update `action_edit_first_source` → rename to `action_edit_source` and use `self._source_cursor`:

```python
    def action_edit_source(self) -> None:
        if self._selected is None:
            return
        c = self._config.collections[self._selected]
        if not c.sources:
            return
        s = c.sources[self._source_cursor]
        screen = SourceEditScreen(
            title=f"{self._selected} / {self._source_cursor + 1}",
            path=str(s.path),
            includes=list(s.includes),
            excludes=list(s.excludes),
            frontmatter_filter=s.frontmatter_filter,
        )
        idx = self._source_cursor
        self.app.push_screen(screen, callback=lambda r: self._apply_source_edit(idx, r))
```

Update the binding:

```python
        Binding("e", "edit_source", "Edit source", show=True),
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add fnd/tui/collections_screen.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — source cursor + add / remove / edit"
```

---

## Task 6: "Test against pasted frontmatter" affordance

**Files:**
- Modify: `fnd/tui/collections_screen.py:SourceEditScreen`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Append:

```python
@pytest.mark.asyncio
async def test_pasted_frontmatter_match_indicator(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        from textual.widgets import Input, Static, TextArea

        # Type a valid filter.
        filter_input = app.query_one("#source_filter_input", Input)
        filter_input.value = "Course == 'DPwC'"
        await pilot.pause()

        # Paste matching frontmatter.
        sample = app.query_one("#frontmatter_sample", TextArea)
        sample.text = "---\nCourse: DPwC\n---\n"
        await pilot.pause()

        match = app.query_one("#frontmatter_match_status", Static)
        assert "match" in str(match.renderable).lower() and "✓" in str(match.renderable)

        # Now non-matching frontmatter.
        sample.text = "---\nCourse: Other\n---\n"
        await pilot.pause()
        match = app.query_one("#frontmatter_match_status", Static)
        assert "no match" in str(match.renderable).lower() or "✗" in str(match.renderable)
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py::test_pasted_frontmatter_match_indicator -v`
Expected: failure (no `#frontmatter_sample`).

- [ ] **Step 3: Add the TextArea + status to `SourceEditScreen.compose`**

Modify `compose` to include:

```python
            from textual.widgets import TextArea

            yield Static("Test against pasted frontmatter:")
            yield TextArea(
                "",
                id="frontmatter_sample",
                classes="frontmatter_sample",
            )
            yield Static("(no sample)", id="frontmatter_match_status")
```

Add CSS for the TextArea sizing (small, 5 rows):

```css
    #frontmatter_sample { height: 6; margin-bottom: 1; border: round $surface; }
```

Add a handler that runs when either the filter input or the sample TextArea changes:

```python
    def on_text_area_changed(self, ev) -> None:  # noqa: ANN001
        if ev.text_area.id != "frontmatter_sample":
            return
        self._refresh_match_status()

    # Also call _refresh_match_status from on_input_changed when the filter
    # input changes (so changing the filter re-evaluates against the pasted
    # sample).

    def _refresh_match_status(self) -> None:
        from textual.widgets import Input, TextArea
        from fnd.filter_dsl import parse_or_error
        from fnd.frontmatter import (
            FrontmatterParseError,
            read_frontmatter_from_text,
        )

        match = self.query_one("#frontmatter_match_status", Static)
        sample = self.query_one("#frontmatter_sample", TextArea).text
        filter_text = self.query_one("#source_filter_input", Input).value.strip()
        if not sample.strip():
            match.update("(no sample)")
            match.remove_class("match")
            match.remove_class("no_match")
            return
        try:
            fm = read_frontmatter_from_text(sample) or {}
        except FrontmatterParseError as e:
            match.update(f"✗ frontmatter parse error: {e}")
            match.add_class("no_match")
            match.remove_class("match")
            return
        if not filter_text:
            match.update("(no filter — sample is parsed but no predicate)")
            match.remove_class("match")
            match.remove_class("no_match")
            return
        pred, err = parse_or_error(filter_text)
        if err is not None or pred is None:
            match.update(f"✗ filter syntax: col {err.column}" if err else "✗")
            match.add_class("no_match")
            match.remove_class("match")
            return
        if pred(fm):
            match.update("✓ matches filter")
            match.add_class("match")
            match.remove_class("no_match")
        else:
            match.update("✗ no match")
            match.add_class("no_match")
            match.remove_class("match")
```

Update `on_input_changed` to also call `self._refresh_match_status()` after the filter parses successfully (or fails):

```python
    def on_input_changed(self, ev) -> None:
        if ev.input.id != "source_filter_input":
            return
        # ... existing parse-status update ...
        self._refresh_match_status()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add fnd/tui/collections_screen.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — paste-frontmatter live match indicator"
```

---

## Task 7: Save round-trip via `tomlkit`

**Files:**
- Modify: `fnd/config.py` (add `write_collection`)
- Modify: `fnd/tui/collections_screen.py` (`s` action)
- Test: `tests/test_config_write_collection.py` (new)
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test for the helper**

Create `tests/test_config_write_collection.py`:

```python
"""Phase 5.5e-3: write_collection round-trips a CollectionConfig via tomlkit."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fnd.config import (
    CollectionConfig,
    SourceConfig,
    load,
    write_collection,
)


def test_write_creates_collection_in_empty_file(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    cc = CollectionConfig(
        sources=[
            SourceConfig(path=Path("/tmp/notes"), includes=["**/*.md"]),
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    out = load(cfg_path)
    assert out.collection("notes").sources[0].path == Path("/tmp/notes")
    assert out.collection("notes").sources[0].includes == ["**/*.md"]


def test_write_preserves_user_comments(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            # I love this config.
            [defaults]
            # important note
            collection = "notes"

            [[collections.papers.sources]]
            path = "/tmp/papers"
        """),
        encoding="utf-8",
    )
    cc = CollectionConfig(
        sources=[
            SourceConfig(path=Path("/tmp/notes"), includes=["**/*.md"]),
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    text = cfg_path.read_text(encoding="utf-8")
    assert "# I love this config." in text
    assert "# important note" in text
    # papers collection still present
    assert "/tmp/papers" in text
    # notes collection added
    assert "/tmp/notes" in text


def test_write_replaces_existing_collection(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.notes.sources]]
            path = "/tmp/old"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    cc = CollectionConfig(
        sources=[
            SourceConfig(path=Path("/tmp/new"), includes=["**/*.txt"]),
            SourceConfig(path=Path("/tmp/extra"), includes=["**/*.pdf"]),
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    out = load(cfg_path)
    paths = [s.path for s in out.collection("notes").sources]
    assert paths == [Path("/tmp/new"), Path("/tmp/extra")]


def test_write_with_frontmatter_filter(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    cc = CollectionConfig(
        sources=[
            SourceConfig(
                path=Path("/tmp/notes"),
                includes=["**/*.md"],
                frontmatter_filter="Course == 'DPwC'",
            )
        ]
    )
    write_collection(config_path=cfg_path, name="notes", collection=cc)
    out = load(cfg_path)
    s = out.collection("notes").sources[0]
    assert s.frontmatter_filter == "Course == 'DPwC'"
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_config_write_collection.py -v`
Expected: 4 failures — `write_collection` not exported.

- [ ] **Step 3: Implement `write_collection`**

Append to `fnd/config.py`:

```python
def write_collection(
    *,
    config_path: Path,
    name: str,
    collection: CollectionConfig,
) -> None:
    """Replace ``[collections.<name>]`` (and its ``[[sources]]`` array) in
    the TOML at ``config_path``. Comments and unrelated tables are
    preserved via ``tomlkit``. Creates the file and the ``collections``
    table if needed.

    The supplied :class:`CollectionConfig` is the canonical post-validation
    form; this writer emits the new ``[[sources]]`` shape and never the
    legacy flat ``roots = [...]`` shape.
    """
    import tomlkit

    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    collections = doc.setdefault("collections", tomlkit.table())
    new_table = tomlkit.table()
    if collection.ranking_profile != "default":
        new_table["ranking_profile"] = collection.ranking_profile
    sources_aot = tomlkit.aot()
    for source in collection.sources:
        st = tomlkit.table()
        st["path"] = str(source.path)
        if source.includes:
            st["includes"] = list(source.includes)
        if source.excludes:
            st["excludes"] = list(source.excludes)
        if source.follow_symlinks:
            st["follow_symlinks"] = source.follow_symlinks
        if source.frontmatter_filter:
            st["frontmatter_filter"] = source.frontmatter_filter
        sources_aot.append(st)
    new_table["sources"] = sources_aot
    collections[name] = new_table
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
```

- [ ] **Step 4: Run helper tests**

Run: `uv run pytest tests/test_config_write_collection.py -v`
Expected: 4 passed.

- [ ] **Step 5: Failing test for `s` save in the form**

Append to `tests/test_collections_screen.py`:

```python
@pytest.mark.asyncio
async def test_s_persists_changes_to_config_toml(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            # user comment
            [[collections.notes.sources]]
            path = "/tmp/old"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)
    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # In the form, focus the first source and "remove" it (so the diff
        # is non-trivial: 1 source becomes 0).
        await pilot.press("x")
        await pilot.pause()
        # Save.
        await pilot.press("s")
        await pilot.pause()
    # File should reflect 0 sources for notes; comment preserved.
    text = cfg_path.read_text(encoding="utf-8")
    assert "# user comment" in text
    out = load(cfg_path)
    assert len(out.collection("notes").sources) == 0
```

- [ ] **Step 6: Wire `s` save in `CollectionsScreen`**

Add to BINDINGS:

```python
        Binding("s", "save", "Save", show=True),
```

Action method:

```python
    def action_save(self) -> None:
        if self._selected is None:
            return
        from fnd.config import write_collection

        c = self._config.collections[self._selected]
        write_collection(
            config_path=self._config_path,
            name=self._selected,
            collection=c,
        )
        self.app.notify(f"Saved {self._selected}", severity="information")
```

- [ ] **Step 7: Run all tests**

Run: `uv run pytest tests/test_collections_screen.py tests/test_config_write_collection.py -v`
Expected: 14 passed (4 helper + 10 form).

Run: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 8: Commit**

```bash
git add fnd/config.py fnd/tui/collections_screen.py tests/test_config_write_collection.py tests/test_collections_screen.py
git commit -m "feat(tui,config): phase 5.5e-3 — write_collection + s save in the form"
```

---

## Task 8: Auto-reindex on structural save

**Files:**
- Modify: `fnd/tui/collections_screen.py:action_save`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Append:

```python
@pytest.mark.asyncio
async def test_save_triggers_reindex_when_paths_change(
    tmp_path: Path,
    tmp_index_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save that changes a source's path / includes / filter should
    auto-reindex the collection so the index reflects the new scope."""
    notes_a = tmp_path / "a"
    notes_b = tmp_path / "b"
    (notes_a := notes_a).mkdir()
    (notes_b := notes_b).mkdir()
    (notes_a / "x.md").write_text("# x\nblue penguin\n", encoding="utf-8")
    (notes_b / "y.md").write_text("# y\nblue penguin\n", encoding="utf-8")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(f"""
            [[collections.notes.sources]]
            path = "{notes_a}"
            includes = ["**/*.md"]
        """),
        encoding="utf-8",
    )
    monkeypatch.setattr("fnd.config.default_config_path", lambda: cfg_path)
    cfg = load(cfg_path)

    # Build the initial index so the form will detect a "change" on save.
    from fnd.index import build_index_from_config
    build_index_from_config(
        config=cfg.collection("notes"), collection="notes", index_dir=tmp_index_dir
    )

    app = FNDApp(index_dir=tmp_index_dir, config=cfg)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Edit the only source: change its path to notes_b.
        await pilot.press("e")
        await pilot.pause()
        from textual.widgets import Input

        path_input = app.query_one("#source_path_input", Input)
        path_input.value = str(notes_b)
        await pilot.press("ctrl+s")
        await pilot.pause()
        # Save the collection (triggers reindex because path changed).
        await pilot.press("s")
        await pilot.pause()

    # Searcher should now find y.md (notes_b) but not x.md (notes_a).
    from fnd.query import Searcher
    s = Searcher(index_dir=tmp_index_dir)
    paths = {Path(h.path).name for h in s.search("blue penguin", limit=10, collection="notes")}
    assert "y.md" in paths
    assert "x.md" not in paths
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py::test_save_triggers_reindex_when_paths_change -v`
Expected: fail — save doesn't yet reindex.

- [ ] **Step 3: Track the original collection state + diff on save**

Update `CollectionsScreen.__init__` to snapshot the initial collection state per name, so `action_save` can diff:

```python
        # Deep-copy the source list per collection at form open. Compared
        # against the live ``self._config`` on save to decide whether a
        # reindex is needed.
        from copy import deepcopy

        self._initial: dict[str, list[SourceConfig]] = {
            name: deepcopy(c.sources) for name, c in config.collections.items()
        }
```

Add `from fnd.config import SourceConfig` and `from copy import deepcopy` at the module top.

Update `action_save`:

```python
    def action_save(self) -> None:
        if self._selected is None:
            return
        from fnd.config import write_collection
        from fnd.index import build_index_from_config

        c = self._config.collections[self._selected]
        write_collection(
            config_path=self._config_path,
            name=self._selected,
            collection=c,
        )
        # Did anything structural change? If so, reindex synchronously.
        if self._needs_reindex(self._selected):
            self.app.notify(
                f"Reindexing {self._selected}…",
                severity="information",
                timeout=2,
            )
            try:
                n = build_index_from_config(
                    config=c,
                    collection=self._selected,
                    index_dir=self.app._index_dir,  # type: ignore[attr-defined]
                    rebuild=True,
                )
                self.app.notify(
                    f"Indexed {n} chunks for {self._selected}.",
                    severity="information",
                )
            except Exception as e:  # noqa: BLE001 — surface anything to the user
                self.app.notify(f"Reindex failed: {e}", severity="error")
        else:
            self.app.notify(
                f"Saved {self._selected}", severity="information"
            )
        # Refresh our snapshot so subsequent saves diff against the new state.
        from copy import deepcopy
        self._initial[self._selected] = deepcopy(c.sources)

    def _needs_reindex(self, name: str) -> bool:
        prev = self._initial.get(name, [])
        curr = self._config.collections[name].sources
        if len(prev) != len(curr):
            return True
        for a, b in zip(prev, curr, strict=True):
            if (
                a.path != b.path
                or list(a.includes) != list(b.includes)
                or list(a.excludes) != list(b.excludes)
                or a.frontmatter_filter != b.frontmatter_filter
                or a.follow_symlinks != b.follow_symlinks
            ):
                return True
        return False
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add fnd/tui/collections_screen.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — auto-reindex on structural save"
```

---

## Task 9: Delete collection (with confirmation)

**Files:**
- Modify: `fnd/config.py` (add `delete_collection`)
- Modify: `fnd/tui/collections_screen.py`
- Test: `tests/test_config_write_collection.py`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test for the helper**

Append to `tests/test_config_write_collection.py`:

```python
def test_delete_collection_removes_table(tmp_path: Path) -> None:
    from fnd.config import delete_collection

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            # important
            [[collections.papers.sources]]
            path = "/tmp/papers"

            [[collections.notes.sources]]
            path = "/tmp/notes"
        """),
        encoding="utf-8",
    )
    delete_collection(config_path=cfg_path, name="notes")
    out = load(cfg_path)
    assert "papers" in out.collections
    assert "notes" not in out.collections
    text = cfg_path.read_text(encoding="utf-8")
    assert "# important" in text


def test_delete_missing_collection_is_idempotent(tmp_path: Path) -> None:
    from fnd.config import delete_collection

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    # Should not raise.
    delete_collection(config_path=cfg_path, name="absent")
    assert cfg_path.read_text(encoding="utf-8") == ""
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_config_write_collection.py -v`
Expected: 2 new failures.

- [ ] **Step 3: Implement `delete_collection`**

Append to `fnd/config.py`:

```python
def delete_collection(*, config_path: Path, name: str) -> None:
    """Remove ``[collections.<name>]`` and its ``[[sources]]`` array from
    the TOML at ``config_path``. Idempotent: silently no-op if the
    collection (or the file) is absent. Comments and unrelated tables
    are preserved via ``tomlkit``."""
    import tomlkit

    if not config_path.exists():
        return
    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    collections = doc.get("collections")
    if not collections or name not in collections:
        return
    del collections[name]
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
```

- [ ] **Step 4: Failing test for `d` in the form**

Append to `tests/test_collections_screen.py`:

```python
@pytest.mark.asyncio
async def test_d_deletes_with_confirmation(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        # Default selection: coursework. Press 'd' to delete.
        await pilot.press("d")
        await pilot.pause()
        # Confirmation modal asks "Delete 'coursework'? [y/N]". Press y.
        await pilot.press("y")
        await pilot.pause()
        screen = app.screen
        assert "coursework" not in screen._config.collections  # type: ignore[attr-defined]
```

- [ ] **Step 5: Implement `d` action with a tiny confirmation modal**

Append to `fnd/tui/collections_screen.py`:

```python
class _DeleteConfirmScreen(Screen[bool]):
    """Tiny y/N confirmation modal."""

    BINDINGS = [  # noqa: RUF012
        Binding("y,Y", "yes", "Yes", show=True),
        Binding("n,N,escape", "no", "No", show=True),
    ]

    CSS = """
    _DeleteConfirmScreen { align: center middle; background: $surface 80%; }
    #confirm_box { width: 60%; height: auto; border: round $error; padding: 1; background: $surface; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._message),
            Static("[y] yes   [N/Esc] no", classes="footer_hint"),
            id="confirm_box",
        )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
```

Add binding + action to `CollectionsScreen`:

```python
        Binding("d", "delete_collection", "Delete", show=True),
```

```python
    def action_delete_collection(self) -> None:
        if self._selected is None:
            return
        name = self._selected
        screen = _DeleteConfirmScreen(
            f"Delete collection '{name}' and remove its indexed chunks?"
        )
        self.app.push_screen(screen, callback=lambda r: self._on_delete_confirmed(name, r))

    def _on_delete_confirmed(self, name: str, ok: bool | None) -> None:
        if not ok:
            return
        from fnd.config import delete_collection
        from fnd.index import _ensure_index
        from fnd.schema import F_COLLECTION

        # 1. Remove from on-disk config.
        delete_collection(config_path=self._config_path, name=name)
        # 2. Remove from in-memory Config so the form re-renders without it.
        self._config.collections.pop(name, None)
        self._initial.pop(name, None)
        # 3. Drop chunks from the index.
        try:
            index = _ensure_index(self.app._index_dir)  # type: ignore[attr-defined]
            writer = index.writer(heap_size=50_000_000)
            writer.delete_documents(F_COLLECTION, name)
            writer.commit()
            writer.wait_merging_threads()
        except Exception as e:  # noqa: BLE001
            self.app.notify(f"Failed to drop chunks: {e}", severity="error")
        # 4. Update selection.
        names = sorted(self._config.collections.keys())
        self._selected = names[0] if names else None
        self._source_cursor = 0
        self._refresh()
        self.app.notify(f"Deleted {name}", severity="information")
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_collections_screen.py tests/test_config_write_collection.py -v`
Expected: all green; 14 form tests + 6 helper tests.

Run: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 7: Commit**

```bash
git add fnd/config.py fnd/tui/collections_screen.py tests/test_config_write_collection.py tests/test_collections_screen.py
git commit -m "feat(tui,config): phase 5.5e-3 — delete collection with confirmation"
```

---

## Task 10: New collection (inline name prompt)

**Files:**
- Modify: `fnd/tui/collections_screen.py`
- Modify: `tests/test_collections_screen.py`

- [ ] **Step 1: Failing test**

Append:

```python
@pytest.mark.asyncio
async def test_n_creates_new_empty_collection(
    cfg_three_collections: Config, tmp_index_dir: Path
) -> None:
    app = FNDApp(index_dir=tmp_index_dir, config=cfg_three_collections)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        from textual.widgets import Input

        # New-name prompt mounts a single input.
        name_input = app.query_one("#new_collection_name", Input)
        name_input.value = "research"
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert "research" in screen._config.collections  # type: ignore[attr-defined]
        # And research is now the selected collection.
        assert screen._selected == "research"  # type: ignore[attr-defined]
```

- [ ] **Step 2: Verify red**

Run: `uv run pytest tests/test_collections_screen.py::test_n_creates_new_empty_collection -v`
Expected: fail.

- [ ] **Step 3: Implement `n` action with a name-prompt modal**

Append to `fnd/tui/collections_screen.py`:

```python
class _NewCollectionScreen(Screen[str | None]):
    """Tiny name-prompt for creating an empty collection."""

    BINDINGS = [  # noqa: RUF012
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CSS = """
    _NewCollectionScreen { align: center middle; background: $surface 80%; }
    #new_collection_box { width: 60%; height: auto; border: round $accent; padding: 1; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        from textual.widgets import Input

        yield Vertical(
            Static("New collection name:"),
            Input(id="new_collection_name", placeholder="e.g. research"),
            Static("[Enter] create   [Esc] cancel", classes="footer_hint"),
            id="new_collection_box",
        )

    def on_input_submitted(self, ev) -> None:  # noqa: ANN001
        name = ev.value.strip()
        self.dismiss(name or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
```

Add binding + action to `CollectionsScreen`:

```python
        Binding("n", "new_collection", "New", show=True),
```

```python
    def action_new_collection(self) -> None:
        screen = _NewCollectionScreen()
        self.app.push_screen(screen, callback=self._on_new_collection_named)

    def _on_new_collection_named(self, name: str | None) -> None:
        if not name:
            return
        if name in self._config.collections:
            self.app.notify(
                f"Collection {name} already exists.", severity="warning"
            )
            return
        from fnd.config import CollectionConfig

        empty = CollectionConfig(sources=[])
        self._config.collections[name] = empty
        self._initial[name] = []
        self._selected = name
        self._source_cursor = 0
        self._refresh()
        self.app.notify(
            f"Created {name}. Press 'a' to add a source, 's' to save.",
            severity="information",
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_collections_screen.py -v`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add fnd/tui/collections_screen.py tests/test_collections_screen.py
git commit -m "feat(tui): phase 5.5e-3 — new collection (n key) with name prompt"
```

---

## Task 11: Plan §22 close-out + acceptance smoke

**Files:**
- (verification only, plus a small docs update)

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: all green; ~310 tests after this phase (288 baseline from 5.5e-2 close-out + ~20 new for 5.5e-3).

- [ ] **Step 2: Lint + types**

Run: `uv run ruff check fnd tests && uv run pyright`
Expected: clean.

- [ ] **Step 3: Manual smoke (recommended, not blocking)**

If you have a real config + index handy:

1. `uv run fnd tui --collection notes`
2. Press `F3` — Collections form opens.
3. Press `j`/`k` to navigate the list; selected collection shows in the editor pane.
4. Press `e` — source-edit modal opens; tweak the path, see filter parse status; press `ctrl+s`.
5. Paste sample frontmatter into the test pane; watch ✓/✗ update live.
6. Press `s` in the form — save persists; if path changed, auto-reindex runs.
7. Press `n` — create a new collection by name.
8. Press `a` — add a source to it.
9. Press `d` then `y` — delete a collection.
10. Press `Esc` — close the form; main TUI is intact.

Verify `config.toml` round-trip: open the file in `$EDITOR`, confirm comments and unrelated tables are preserved across all the operations above.

- [ ] **Step 4: Update top-level plan §22 (out-of-scope) — drop the "TUI Collection CRUD" deferral**

Find the §22 line about "Collection CRUD UI inside the TUI" or equivalent in the top-level fnd design plan. Strike it through (or remove) — it's now shipped.

If the plan file isn't easily editable here, skip this step and note it for the user to apply manually.

- [ ] **Step 5: Update task tracker**

- Mark task #20 (Phase 5.5e parent) as `completed`.
- Mark task #44 (5.5e-2 acceptance) — already completed.
- Mark all 5.5e-3 task entries as completed.

- [ ] **Step 6: Final close-out commit (if docs were touched)**

If you didn't touch any docs in step 4, no commit needed. Otherwise:

```bash
git add docs/
git commit -m "docs: phase 5.5e-3 close-out — drop TUI CRUD deferral from plan"
```

---

## Self-review notes (for the executor)

- **Spec coverage:**
  - F3 / `:collections` action → Task 1
  - Collections list + editor pane → Tasks 2, 3
  - Source-edit modal with parse-status → Task 4
  - Source cursor + add/remove → Task 5
  - "Test against pasted frontmatter" → Task 6
  - Save round-trip via tomlkit → Task 7
  - Auto-reindex on structural change → Task 8
  - Delete collection with confirm → Task 9
  - New collection inline → Task 10
  - Acceptance gates → Task 11

- **Type / name consistency:**
  - `CollectionsScreen(Screen[None])` — pushed via `app.push_screen`, dismissed with `None`.
  - `SourceEditScreen(Screen[dict | None])` — dismissed with edited fields or None on cancel.
  - `_DeleteConfirmScreen(Screen[bool])`, `_NewCollectionScreen(Screen[str | None])`.
  - `write_collection(*, config_path, name, collection)` and `delete_collection(*, config_path, name)` are both kw-only.
  - `_needs_reindex(name)` returns True when path / includes / excludes / filter / follow_symlinks differ.
  - The auto-reindex calls `_ensure_index(force=True)` indirectly via `build_index_from_config(rebuild=True)` — same path the migrate helper uses.

- **Out of scope (explicitly deferred to 5.5e-3.x):**
  - File-picker for `[browse]` path button — mockup shows it; implementation is non-trivial.
  - Per-collection ranking-profile dropdown — edit via `config edit` for now.
  - Drag-reorder of sources — not a v1 ask.
  - Multi-collection bulk operations.

- **Testing notes:**
  - All form tests use `pytest.mark.asyncio` + `app.run_test()` + `pilot`.
  - Some tests inspect `app._config.collections[...]` directly — these are internal but stable enough across the phase.
  - `monkeypatch.setattr("fnd.config.default_config_path", ...)` is the established pattern; reuse it.
  - The auto-reindex test uses real on-disk md files in `tmp_path` and a real Tantivy index in `tmp_index_dir`. Slow-ish (~1s) but solid.

- **Schema-bump migration:** unchanged. No new schema fields in this phase. `_ensure_index(force=True)` is reused for the auto-reindex path.

- **Saved searches / history:** unchanged. No interaction with the new form.

- **Theming:** the new screens use the existing tokyo-night theme via Textual's `$accent` / `$surface` / `$error` / `$success` / `$panel` variables. No CSS variables added.
