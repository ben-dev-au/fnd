"""With the preview pane focused, the scroll keys move a flat file's buffer, which
scrolls itself inside the pane, as they move a structural preview."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.line_buffer import LineBufferPreview
from tests._pilot_wait import safe_press, wait_until


@pytest.fixture
def flat_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    lines = [f"line {i} filler words about nothing much" for i in range(400)]
    lines[200] = "line 200 zebra marker"
    (docs / "notes.txt").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[docs], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


async def _flat_buffer(pilot: Any, app: FNDApp) -> LineBufferPreview:
    await wait_until(
        pilot,
        lambda: (
            app._flat.active_buffer is not None
            and app._preview.is_painted()
            and not app._preview.pipeline_busy()
            and not app._preview_scroll.is_settling
        ),
        timeout=20.0,
        message="the flat preview never settled",
    )
    buf = app._flat.active_buffer
    assert isinstance(buf, LineBufferPreview)
    app.action_focus_preview_pane()
    await wait_until(pilot, lambda: app.query_one("#preview_pane").has_focus)
    return buf


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "moves"),
    [
        ("j", "down"),
        ("down", "down"),
        ("pagedown", "down"),
        ("end", "down"),
        ("k", "up"),
        ("up", "up"),
        ("pageup", "up"),
        ("home", "up"),
    ],
)
async def test_a_scroll_key_moves_the_flat_buffer(flat_index: Path, key: str, moves: str) -> None:
    """Each scroll key moves the flat buffer the way it moves a structural preview."""
    app = FNDApp(index_dir=flat_index, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:
        buf = await _flat_buffer(pilot, app)
        start = buf.scroll_y
        assert 0 < start < buf.max_scroll_y, "the match should land mid-file"
        await safe_press(pilot, key)
        if moves == "down":
            await wait_until(
                pilot, lambda: buf.scroll_target_y > start, message=f"{key} did not scroll down"
            )
        else:
            await wait_until(
                pilot, lambda: buf.scroll_target_y < start, message=f"{key} did not scroll up"
            )


@pytest.mark.asyncio
async def test_reading_view_steps_the_flat_buffer_three_lines(flat_index: Path) -> None:
    """In Reading View a scroll key moves a flat file as far as a structural one."""
    app = FNDApp(index_dir=flat_index, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:
        buf = await _flat_buffer(pilot, app)
        restores = app._preview_scroll.restores_completed
        app.action_toggle_reading_mode()
        await wait_until(
            pilot,
            lambda: app._reading_mode and app._preview_scroll.restores_completed > restores,
            message="Reading View never restored its position",
        )
        start = buf.scroll_target_y
        await safe_press(pilot, "down")
        await wait_until(
            pilot, lambda: buf.scroll_target_y == start + 3, message="not a three-line step"
        )


@pytest.mark.asyncio
async def test_a_key_scroll_hands_the_flat_preview_back_to_the_reader(flat_index: Path) -> None:
    """A scroll key on a flat file releases the landing, as it does on a structural one."""
    app = FNDApp(index_dir=flat_index, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:
        await _flat_buffer(pilot, app)
        assert app._preview_scroll.is_armed, (
            "the landing is not holding the view, so this proves nothing"
        )
        await safe_press(pilot, "j")
        await wait_until(
            pilot,
            lambda: not app._preview_scroll.is_armed,
            message="the key scroll did not release",
        )


@pytest.mark.asyncio
async def test_a_click_on_the_flat_preview_focuses_the_pane_its_keys_drive(
    flat_index: Path,
) -> None:
    """A click on a flat file focuses the preview pane, so its keys scroll and release."""
    app = FNDApp(index_dir=flat_index, initial_query="zebra")
    async with app.run_test(size=(120, 30)) as pilot:
        buf = await _flat_buffer(pilot, app)
        assert app._preview_scroll.is_armed, "the landing is not holding the view"
        app.query_one("#results_pane").focus()
        await wait_until(pilot, lambda: not app.query_one("#preview_pane").has_focus)
        await pilot.click(buf)
        await wait_until(
            pilot,
            lambda: app.query_one("#preview_pane").has_focus,
            message="the click focused elsewhere",
        )
        start = buf.scroll_target_y
        await safe_press(pilot, "down")
        await wait_until(pilot, lambda: buf.scroll_target_y > start, message="down did not scroll")
        await wait_until(
            pilot,
            lambda: not app._preview_scroll.is_armed,
            message="the key scroll did not release",
        )
