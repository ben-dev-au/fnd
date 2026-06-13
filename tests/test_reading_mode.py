"""Reading mode hides the sidebar so the preview fills the full terminal
width — a normal terminal text selection then covers only the preview
(clean copy for text-to-speech), and it reads distraction-free. Toggling
again restores the sidebar. Reading mode also owns mouse capture: it
hands the terminal back its mouse on entry (so native selection / TTS
work) and re-captures on exit (so click-to-focus / hover wheel-scroll
return)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from textual.pilot import Pilot

from fnd.config import Config
from fnd.tui import FNDApp


@contextmanager
def _record_mouse_calls(app: FNDApp) -> Generator[list[str]]:
    """Temporarily stub the live driver's mouse-support hooks with
    recorders, then restore originals on exit so the patch can't leak
    into anything that shares the same driver lifetime. Pure recorders
    (no delegation): the real hooks write mouse-tracking escape
    sequences and we don't want those side effects during tests.
    Patching just the two hooks (not swapping ``_driver`` wholesale)
    keeps Textual's timer/bell code working — they read other driver
    attrs like ``is_headless``."""
    calls: list[str] = []
    driver = app._driver  # type: ignore[attr-defined]
    assert driver is not None
    sentinel = object()
    original_enable = getattr(driver, "_enable_mouse_support", sentinel)
    original_disable = getattr(driver, "_disable_mouse_support", sentinel)

    def _enable() -> None:
        calls.append("enable")

    def _disable() -> None:
        calls.append("disable")

    driver._enable_mouse_support = _enable  # type: ignore[attr-defined]
    driver._disable_mouse_support = _disable  # type: ignore[attr-defined]
    try:
        yield calls
    finally:
        if original_enable is sentinel:
            del driver._enable_mouse_support  # type: ignore[attr-defined]
        else:
            driver._enable_mouse_support = original_enable  # type: ignore[attr-defined]
        if original_disable is sentinel:
            del driver._disable_mouse_support  # type: ignore[attr-defined]
        else:
            driver._disable_mouse_support = original_disable  # type: ignore[attr-defined]


def test_reading_mode_action_registered() -> None:
    from fnd.tui.actions import REGISTRY

    action = next(a for a in REGISTRY if a.id == "toggle_reading_mode")
    assert action.default_key == "z"
    assert action.footer_label == "Reading View"


@pytest.mark.asyncio
async def test_reading_mode_toggles_sidebar_visibility() -> None:
    app = FNDApp(config=Config())
    async with app.run_test(size=(100, 30)) as pilot:
        with _record_mouse_calls(app) as calls:
            column = app.query_one("#results_column")
            preview = app.query_one("#preview_pane")
            assert column.display is True
            assert app._reading_mode is False
            assert preview.has_class("-reading") is False

            app.action_toggle_reading_mode()
            await pilot.pause()
            assert app._reading_mode is True
            assert column.display is False
            # Border/padding dropped (via class) so selection copies no frame,
            # and the pane's own scrollbar is zeroed (the inner buffer keeps the
            # match-marker bar) so reading view shows no duplicate scrollbar.
            assert preview.has_class("-reading") is True
            assert preview.styles.scrollbar_size_vertical == 0
            # Mouse capture released so the terminal owns selection / TTS.
            assert calls[-1] == "disable"

            app.action_toggle_reading_mode()
            await pilot.pause()
            assert app._reading_mode is False
            assert column.display is True
            assert preview.has_class("-reading") is False
            assert preview.styles.scrollbar_size_vertical == 1
            # Mouse capture restored on exit → hover wheel-scroll back.
            assert calls[-1] == "enable"


@pytest.mark.asyncio
async def test_escape_exits_reading_mode() -> None:
    app = FNDApp(config=Config())
    async with app.run_test(size=(100, 30)) as pilot:
        with _record_mouse_calls(app) as calls:
            app.action_toggle_reading_mode()
            await pilot.pause()
            assert app._reading_mode is True
            assert calls[-1] == "disable"

            app.action_escape_back()
            await pilot.pause()
            assert app._reading_mode is False
            assert app.query_one("#results_column").display is True
            # Esc-exit takes the same path through action_toggle_reading_mode,
            # so mouse capture is restored.
            assert calls[-1] == "enable"


def _build_doc(tmp_path: Path, tmp_index_dir: Path, n_sections: int) -> tuple[Config, Path]:
    """A markdown file big enough to exceed the visible window, indexed."""
    from fnd.config import load
    from fnd.index import build_index

    notes = tmp_path / "notes"
    notes.mkdir()
    lines = ["# Doc", ""]
    for i in range(n_sections):
        lines += [f"## Section {i}", f"MARK{i} body." + (" target." if i == 30 else ""), ""]
    (notes / "doc.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[[collections.notes.sources]]\npath = "{notes}"\n', encoding="utf-8")
    return load(cfg_path), Path(tmp_index_dir)


async def _load_full(app: FNDApp, pilot: Pilot[None]) -> None:
    import asyncio

    app._search.run("target")
    await asyncio.sleep(1.0)
    g = app._search.groups[0]
    app._preview.render_full_doc(g.parent_id, focus_chunk_seq=30)
    for _ in range(80):
        await asyncio.sleep(0.05)
        a = app._preview.active
        if a is not None and getattr(a, "is_complete", False):
            break
    await pilot.pause()


@pytest.mark.asyncio
async def test_reading_view_prunes_full_mount_to_window(
    tmp_path: Path, tmp_index_dir: Path
) -> None:
    """Reading View is pure scroll-nav, so the full-document mount kept for
    instant in-file match-jumps buys nothing. Entering reading view prunes to
    the visible window (cheaper toggle + scroll); the on-screen content must
    stay put (prune is scroll-compensated)."""
    cfg, idx = _build_doc(tmp_path, tmp_index_dir, 80)
    app = FNDApp(index_dir=idx, config=cfg, collection="notes")
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await _load_full(app, pilot)
        pane = app.query_one("#preview_pane")

        def visible_marks() -> set[str]:
            from fnd.tui.widgets.markdown import FNDMarkdown

            top, bot = pane.scroll_y, pane.scroll_y + pane.size.height
            out: set[str] = set()
            for w in pane.query(FNDMarkdown):
                src = getattr(w, "_markdown", "") or ""
                try:
                    if w.virtual_region.y < bot and w.virtual_region.y + w.size.height > top:
                        out |= {t for t in src.split() if t.startswith("MARK")}
                except Exception:
                    pass
            return out

        active = app._preview.active
        assert active is not None
        mounted_before = len(active.mounted_indices)
        vis_before = visible_marks()
        app.action_toggle_reading_mode()
        await pilot.pause()
        mounted_after = len(active.mounted_indices)
        assert mounted_after < mounted_before, "reading view did not prune to a window"
        # The same content is still on screen — prune compensated the scroll.
        assert vis_before & visible_marks(), "visible content shifted/lost on prune"


@pytest.mark.asyncio
async def test_reading_view_scroll_step_is_larger(tmp_path: Path, tmp_index_dir: Path) -> None:
    """A scroll-key press advances more than one line in Reading View (the
    mouse-off arrow-flood means each key = a repaint; a bigger step covers a
    wheel/momentum burst in fewer repaints). Normal preview keeps 1 line.

    Uses a SHORT but tall document (few chunks → not pruned by reading view,
    so the comparison isolates the step size from the windowing path) with
    long bodies so there is ample room to scroll from the top.
    """
    import asyncio

    from fnd.config import load
    from fnd.index import build_index

    notes = tmp_path / "notes"
    notes.mkdir()
    lines = ["# Doc", ""]
    for i in range(4):
        lines += [f"## Section {i}", "target." if i == 0 else f"s{i}."]
        lines += [f"line {i}.{j} of body text here." for j in range(40)]
        lines += [""]
    (notes / "doc.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[[collections.notes.sources]]\npath = "{notes}"\n', encoding="utf-8")

    from fnd.tui.preview_scrollbar import MatchAwareScroll

    app = FNDApp(index_dir=Path(tmp_index_dir), config=load(cfg_path), collection="notes")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        app._search.run("target")
        await asyncio.sleep(1.0)
        g = app._search.groups[0]
        app._preview.render_full_doc(g.parent_id, focus_chunk_seq=0)
        for _ in range(60):
            await asyncio.sleep(0.05)
            a = app._preview.active
            if a is not None and getattr(a, "is_complete", False):
                break
        await pilot.pause()
        pane = app.query_one("#preview_pane", MatchAwareScroll)
        pane.focus()
        # Compare scroll_target_y deltas (the intended destination, not the
        # animated scroll_y mid-flight). scroll_to defers the target update to
        # the next refresh, so pause() after each press before reading it.
        # Normal preview from the top: one line per press.
        pane.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        base = pane.scroll_target_y
        pane.action_scroll_down()
        await pilot.pause()
        normal_step = pane.scroll_target_y - base
        assert normal_step == 1, f"normal step should be 1 line, got {normal_step}"
        # Reading view from the top: larger step (not pruned — few chunks).
        app.action_toggle_reading_mode()
        await pilot.pause()
        pane.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        base = pane.scroll_target_y
        pane.action_scroll_down()
        await pilot.pause()
        reading_step = pane.scroll_target_y - base
        assert reading_step > normal_step, (
            f"reading-view step ({reading_step}) should exceed normal ({normal_step})"
        )


def test_apply_mouse_capture_is_safe_without_driver_hooks() -> None:
    """Helper must no-op when the driver lacks the private hooks (headless
    test drivers, future driver changes) instead of raising — reading-mode
    toggling stays usable even in such environments."""
    app = FNDApp(config=Config())
    app._driver = object()  # type: ignore[assignment]
    app._apply_mouse_capture(True)
    app._apply_mouse_capture(False)
