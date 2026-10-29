"""The ``inflight_target`` latch coalesces a load still landing, never a deliberate
re-select of one that has settled (#60)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Tree

from fnd.config import Config, Defaults, RankingProfileConfig
from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview.presenter import target_from_node_data
from tests._pilot_wait import run_search, wait_until


@pytest.fixture
def mixed_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    notes = tmp_path / "mixed"
    notes.mkdir()
    for label in ("north", "south"):
        body = ["# Top heading", "latch-anchor opening line."]
        for i in range(30):
            body.extend([f"## Section {i}", f"Filler text in section {i}."])
        body.extend(["## Closing section", f"latch-anchor closing line in {label}."])
        (notes / f"{label}.md").write_text("\n".join(body), encoding="utf-8")
    lines = [f"Plain line {i}." for i in range(60)]
    lines.insert(30, "latch-anchor in the plain file.")
    (notes / "plain.txt").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="mixed")
    return tmp_index_dir


def _row_lines(tree: Tree[dict[str, Any]]) -> dict[tuple[str, int], int]:
    lines: dict[tuple[str, int], int] = {}
    for line in range(tree.last_line + 1):
        node = tree.get_node_at_line(line)
        if node is None or (node.is_expanded and node.children):
            continue
        target = target_from_node_data(node.data)
        if target is not None:
            lines.setdefault(target, line)
    return lines


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [".txt", "north.md"])
async def test_enter_on_the_settled_row_loads_it_again(mixed_index: Path, suffix: str) -> None:
    """Enter on the row that just loaded loads it again, flat or structural."""
    cfg = Config(
        defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
        ranking={"default": RankingProfileConfig()},
    )
    app = FNDApp(index_dir=mixed_index, config=cfg, collection="mixed")
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await run_search(pilot, app, "latch-anchor")
        preview = app._preview
        tree: Tree[dict[str, Any]] = app.query_one("#results_pane", Tree)
        for node in list(tree.root.children):
            node.expand()
        await pilot.pause()
        by_path = {g.parent_id: g.path for g in app._search.groups}
        target = next(t for t in _row_lines(tree) if by_path[t[0]].endswith(suffix))
        tree.focus()
        tree.cursor_line = _row_lines(tree)[target]

        def _settled() -> bool:
            return (
                preview.showing_parent() == target[0]
                and preview.is_painted()
                and not preview.pipeline_busy()
            )

        await wait_until(pilot, _settled, timeout=30.0, message="preview never settled")
        renders: list[tuple[str, int]] = []
        real_render = preview.render_full_doc

        def _spy(parent_id: str, *, focus_chunk_seq: int) -> None:
            renders.append((parent_id, focus_chunk_seq))
            real_render(parent_id, focus_chunk_seq=focus_chunk_seq)

        preview.render_full_doc = _spy  # type: ignore[method-assign]
        await pilot.press("enter")
        await wait_until(pilot, _settled, timeout=30.0, message="re-select never settled")
        assert renders == [target]
