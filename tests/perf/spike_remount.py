"""Spike — does a Widget instance survive remove() + re-mount()?

Tested 2026-05-15 on Textual 8.2.5: PASS for a leaf widget (Static
survives detach + re-attach with state intact). FAIL for a parent
with children (see spike_offdom_mount.py): Textual's _prune is
recursive and tears down the child tree. So the "detach to cache,
re-attach on activate" pattern is NOT viable for caching
PreviewContainers — Stage 3's literal Screen-per-file would need
actual Screens, which don't fit acorn's side-by-side layout.

Run with:
    ./.venv/bin/python tests/perf/spike_remount.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.widgets import Static  # noqa: E402


class HostApp(App[None]):
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="host")


async def main() -> int:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        host = app.query_one("#host", VerticalScroll)

        cached = Static("hello", id="cached")
        cached_id = id(cached)
        await host.mount(cached)
        await pilot.pause()

        print(f"mounted: parent={cached.parent is host} repr={str(cached.render())[:60]!r}")

        await cached.remove()
        await pilot.pause()
        print(
            f"removed: parent={cached.parent}  same_obj={id(cached) == cached_id}  "
            f"repr={str(cached.render())[:60]!r}"
        )

        try:
            await host.mount(cached)
            await pilot.pause()
            print(f"remounted: parent={cached.parent is host} repr={str(cached.render())[:60]!r}")
            ok = True
        except Exception as e:
            print(f"remount failed: {type(e).__name__}: {e}")
            ok = False

        print()
        print("RESULT:", "PASS — detach/re-attach pattern is viable." if ok else "FAIL.")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
