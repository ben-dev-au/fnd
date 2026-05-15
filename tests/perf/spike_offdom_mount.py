"""Spike — can we build a widget tree off-DOM and mount the root later?

Result 2026-05-15 (Textual 8.2.5):
- Pattern A (mount-then-detach): re-attach succeeds but children are
  GONE (Textual's _prune is recursive; remove() destroys the tree).
- Pattern B (mount into unmounted parent): MountError. Textual
  refuses to mount into an unmounted parent.
Combined with spike_remount.py: caching widget trees off-DOM is not
viable. Stage 3's literal screen-per-file would need actual Textual
Screens, which don't fit acorn's side-by-side layout.

Run with:
    ./.venv/bin/python tests/perf/spike_offdom_mount.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Container, VerticalScroll  # noqa: E402
from textual.widgets import Static  # noqa: E402


class HostApp(App[None]):
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="host")


async def main() -> int:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        host = app.query_one("#host", VerticalScroll)

        # Pattern A: mount-then-detach. Mount the container hidden,
        # let chunks land, then remove from DOM but keep the ref.
        cont = Container(id="warm_a")
        await host.mount(cont)
        await pilot.pause()
        await cont.mount(Static("a-1"))
        await cont.mount(Static("a-2"))
        await pilot.pause()
        a_children_before = len(list(cont.children))
        print(f"A: mounted, children={a_children_before}")
        await cont.remove()
        await pilot.pause()
        a_children_after = len(list(cont.children))
        print(f"A: detached, children={a_children_after} parent={cont.parent}")
        # Re-attach
        await host.mount(cont)
        await pilot.pause()
        a_children_remounted = len(list(cont.children))
        print(
            f"A: re-attached, children={a_children_remounted} parent_is_host={cont.parent is host}"
        )

        # Pattern B: mount children into a never-mounted parent.
        unmounted = Container(id="warm_b")
        try:
            await unmounted.mount(Static("b-1"))
            print(
                f"B: mounted child into unmounted parent OK; children={len(list(unmounted.children))}"
            )
        except Exception as e:
            print(f"B: mount into unmounted parent FAILED: {type(e).__name__}: {e}")

        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
