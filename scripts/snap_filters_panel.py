"""Capture an SVG screenshot showing the Filters panel in action.

Run via: ``uv run python scripts/snap_filters_panel.py [output.svg]``

Renders the TUI with the filters panel expanded and a couple of values
selected so we can verify the layout reads cleanly next to the
collections panel.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

from textual.widgets import Tree

from fnd.config import load
from fnd.index import build_index
from fnd.tui import FNDApp


async def _snap(out_path: Path) -> None:
    work = Path("/tmp/__fnd_snap_filters")
    work.mkdir(parents=True, exist_ok=True)
    cfg_path = work / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [[collections.DPC.sources]]
            path = "/tmp/__fnd_snap_filters/dpc"

            [[collections.wine.sources]]
            path = "/tmp/__fnd_snap_filters/wine"
        """),
        encoding="utf-8",
    )
    dpc = work / "dpc"
    dpc.mkdir(exist_ok=True)
    wine = work / "wine"
    wine.mkdir(exist_ok=True)
    (dpc / "templates.md").write_text(
        "# Templates\n\nstrategy pattern templates here.\n",
        encoding="utf-8",
    )
    (dpc / "iterators.md").write_text(
        "# Iterators\n\ncontainers and iterators templates pattern.\n",
        encoding="utf-8",
    )
    (wine / "Yalumba.md").write_text("# Yalumba\nA wine note.\n", encoding="utf-8")

    idx = work / "index"
    if idx.exists():
        import shutil

        shutil.rmtree(idx)
    idx.mkdir()
    build_index(roots=[dpc], index_dir=idx, collection="DPC")
    build_index(roots=[wine], index_dir=idx, collection="wine")
    cfg = load(cfg_path)

    app = FNDApp(index_dir=idx, config=cfg, collection="DPC", initial_query="templates")
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        # Pre-select two file kinds and a date so the panel renders a
        # realistic active state.
        app._filter_kinds = ["pdf", "md"]
        app._filter_date = "week"
        app._refresh_filters_panel()
        await pilot.pause()
        ftree = app.query_one("#filters_panel_tree", Tree)
        # Expand both branches so all rows are visible in the snapshot.
        for branch in ftree.root.children:
            branch.expand()
        ftree.focus()
        await pilot.pause()
        app.save_screenshot(filename=str(out_path))


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "/tmp/fnd_filters_panel.svg")
    asyncio.run(_snap(out))
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
