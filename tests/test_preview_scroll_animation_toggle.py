"""The glide is a setting, so a landing can be watched without motion."""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import Config
from fnd.index import build_index
from fnd.tui import FNDApp
from tests._pilot_wait import wait_until

DOC = "# Notes\n\n" + "\n\n".join(f"Paragraph {i} about templates." for i in range(40))


@pytest.fixture
def notes_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text(DOC, encoding="utf-8")
    (notes / "b.md").write_text(DOC, encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


async def _armed_animate(index: Path, *, glide: bool) -> bool:
    cfg = Config()
    cfg.defaults.preview_scroll_animation = glide
    app = FNDApp(index_dir=index, config=cfg, collection="notes", initial_query="templates")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: app._preview_scroll.anchor is not None,
            timeout=15.0,
            message="no anchor was ever armed",
        )
        # A same-file re-navigation is the only case that glides at all; a fresh
        # file is always a cut.
        seqs = sorted(getattr(app._preview, "chunk_widgets", {}) or {})
        assert seqs, "nothing mounted to re-navigate within"
        anchor = app._preview_scroll.anchor
        assert anchor is not None
        app._preview.render_full_doc(anchor.parent_id, focus_chunk_seq=seqs[-1])
        armed = app._preview_scroll.anchor
        assert armed is not None
        return armed.animate


@pytest.mark.asyncio
async def test_a_mounted_target_glides_by_default(notes_index: Path) -> None:
    assert await _armed_animate(notes_index, glide=True) is True


@pytest.mark.asyncio
async def test_the_setting_turns_the_glide_into_a_cut(notes_index: Path) -> None:
    assert await _armed_animate(notes_index, glide=False) is False
