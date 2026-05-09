"""Capture an SVG screenshot of the TUI in a known state.

Run via: ``uv run python scripts/snap_tui.py [output.svg]``

Builds a tiny 2-collection index with realistic content lengths
(forces the same overflow / scrollbar behaviour the user sees in
their real corpus) and saves an SVG of the rendered screen so we
can inspect colors, layout, and overflow without needing the user
to paste a screenshot.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

from acorn.config import load
from acorn.index import build_index
from acorn.tui import AcornApp


async def _snap(out_path: Path) -> None:
    work = Path("/tmp/__acorn_snap")
    work.mkdir(parents=True, exist_ok=True)
    cfg_path = work / "config.toml"
    cfg_path.write_text(
        textwrap.dedent("""
            [collections.DPC]
            roots = ["/tmp/__acorn_snap/dpc"]

            [collections.wine]
            roots = ["/tmp/__acorn_snap/wine"]
        """),
        encoding="utf-8",
    )
    dpc = work / "dpc"
    dpc.mkdir(exist_ok=True)
    wine = work / "wine"
    wine.mkdir(exist_ok=True)
    long_names = [
        "DPC Wk8 Notes - Templates, Strategy Pattern & C++ Streams.md",
        "DPC Wk9 Notes.md",
        "C++ Cheatsheet.md",
        "DPC Wk3 Notes - Containers, Iterators, Algorithms.md",
        "26S1DPCWk8 - week 8 - templates.pdf-summary.md",
        "CMakeLists.txt.md",
        "Head First Design Patterns_ Building Extensible Software.md",
        "out_fixed.pdf-extract.md",
    ]
    for name in long_names:
        (dpc / name).write_text(
            f"# {name}\n\nThis file mentions templates throughout. "
            "It is a sample with enough body content to exercise the "
            "preview pane rendering for templates and design patterns.\n",
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

    app = AcornApp(index_dir=idx, config=cfg, collection="DPC", initial_query="templates")
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        # Land focus somewhere sensible so the active-pane border shows.
        from textual.widgets import Tree

        tree = app.query_one("#results_pane", Tree)
        tree.focus()
        await pilot.pause()
        app.save_screenshot(filename=str(out_path))


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "/tmp/acorn_snap.svg")
    asyncio.run(_snap(out))
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
