"""Stage 0 spike — verify Textual's compositor only walks the active
screen.

Hypothesis (from Claude 4.7's research): `App._screen_stacks` keeps
suspended Screens alive in Python, but the compositor walk runs against
``app.screen`` (the top of the active stack) only. If that holds, then
screen-per-file LRU is a viable ghost-mount for the preview cache (see
docs/PREVIEW_DOM_PLAN.md §Stage 3 / P1).

Measurement strategy:

1. Build a Textual App with two screens (A and B), each containing
   200 ``CountingStatic`` widgets that increment a per-screen counter
   on every ``render()`` call.
2. Push A. Drive a few refresh ticks. Read:
     - ``app.screen._compositor.full_map`` size
     - ``CountingStatic`` total renders (from each screen's counter)
3. Push B (A suspends).
4. Drive a few refresh ticks. Read the same.
5. Pop back to A. Re-measure.

Pass criterion: while screen B is active, A's widgets register zero
new renders AND ``app.screen._compositor.full_map`` size corresponds
to B's widget count only (not A+B).

Result (2026-05-15 on Textual 8.2.5):
    PASS — delta while B active: renders_A=+0, renders_B=+424.
    compositor full_map sized for active screen only (A: 203, B: 203).
    P1 (screen-per-file LRU, plan §Stage 3) is therefore viable.

Run with:
    ./.venv/bin/python tests/perf/spike_screen_compositor.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

# Make `acorn` importable even though we don't use it (keeps the
# script self-contained and runnable from anywhere via the venv).
_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from textual.app import App  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.screen import Screen  # noqa: E402
from textual.widgets import Static  # noqa: E402

WIDGETS_PER_SCREEN = 200

# Per-screen render counters, keyed by screen name.
RENDER_COUNTS: Counter[str] = Counter()


class CountingStatic(Static):
    """Static that records how many times Textual asked it to render."""

    def __init__(self, label: str, screen_name: str) -> None:
        super().__init__(label)
        self._screen_name = screen_name

    def render(self):
        RENDER_COUNTS[self._screen_name] += 1
        return super().render()


class ProbeScreen(Screen[None]):
    """Vertical stack of CountingStatic rows so we have something the
    compositor has to lay out per refresh."""

    def __init__(self, name: str, n_widgets: int = WIDGETS_PER_SCREEN) -> None:
        super().__init__(name=name)
        self._n_widgets = n_widgets

    def compose(self):
        with VerticalScroll():
            for i in range(self._n_widgets):
                yield CountingStatic(f"row-{i:03d} on {self.name}", self.name or "?")


class SpikeApp(App[None]):
    """Two-screen app to test compositor scope on push/pop."""

    def on_mount(self) -> None:
        # Initial screen "A" is installed so we can later switch back.
        self.install_screen(ProbeScreen("A"), name="A")
        self.install_screen(ProbeScreen("B"), name="B")
        self.push_screen("A")


def _compositor_size(app: App[None]) -> int:
    """How many widgets does the active screen's compositor see?"""
    try:
        comp = app.screen._compositor  # type: ignore[attr-defined]
    except Exception:
        return -1
    # Trigger a fresh layout walk if needed.
    return len(comp.full_map)


def _dom_size(app: App[None]) -> int:
    """How many widgets are reachable from the active screen via walk_children?"""
    try:
        return sum(1 for _ in app.screen.walk_children(with_self=False))
    except Exception:
        return -1


def _snapshot(app: App[None], phase: str) -> dict[str, object]:
    return {
        "phase": phase,
        "active_screen": getattr(app.screen, "name", None),
        "screen_stack": [getattr(s, "name", "?") for s in app.screen_stack],
        "compositor_full_map_size": _compositor_size(app),
        "active_walk_children_size": _dom_size(app),
        "renders_A": RENDER_COUNTS["A"],
        "renders_B": RENDER_COUNTS["B"],
    }


def _print(snap: dict[str, object]) -> None:
    print(
        f"  [{snap['phase']:<32}] "
        f"active={snap['active_screen']!s:<3} "
        f"stack={snap['screen_stack']} "
        f"compositor={snap['compositor_full_map_size']:>4} "
        f"walk_children={snap['active_walk_children_size']:>4} "
        f"renders A={snap['renders_A']:>5} B={snap['renders_B']:>5}"
    )


async def main() -> int:
    app = SpikeApp()
    async with app.run_test(size=(80, 24)) as pilot:
        # Initial mount; A is on top of the stack.
        await pilot.pause()
        await asyncio.sleep(0.1)
        await pilot.pause()
        snap_a1 = _snapshot(app, "A active, first paint")
        _print(snap_a1)

        # Drive a few refresh ticks while A is active.
        for _ in range(5):
            app.screen.refresh()
            await pilot.pause()
            await asyncio.sleep(0.02)
        snap_a2 = _snapshot(app, "A active, after 5 refreshes")
        _print(snap_a2)

        renders_a_before_push = (RENDER_COUNTS["A"], RENDER_COUNTS["B"])

        # Push B; A should suspend.
        app.push_screen("B")
        await pilot.pause()
        await asyncio.sleep(0.1)
        await pilot.pause()
        snap_b1 = _snapshot(app, "B pushed, first paint")
        _print(snap_b1)

        # Drive 5 refresh ticks against B and observe A's render count.
        for _ in range(5):
            app.screen.refresh()
            await pilot.pause()
            await asyncio.sleep(0.02)
        snap_b2 = _snapshot(app, "B active, after 5 refreshes")
        _print(snap_b2)

        renders_a_after_push = (RENDER_COUNTS["A"], RENDER_COUNTS["B"])
        a_render_delta = renders_a_after_push[0] - renders_a_before_push[0]
        b_render_delta = renders_a_after_push[1] - renders_a_before_push[1]
        print(f"  delta while B active: renders_A=+{a_render_delta} renders_B=+{b_render_delta}")

        # Pop B; A should re-activate.
        app.pop_screen()
        await pilot.pause()
        await asyncio.sleep(0.1)
        await pilot.pause()
        snap_a3 = _snapshot(app, "A reactivated after pop")
        _print(snap_a3)

        renders_at_pop = (RENDER_COUNTS["A"], RENDER_COUNTS["B"])
        for _ in range(5):
            app.screen.refresh()
            await pilot.pause()
            await asyncio.sleep(0.02)
        snap_a4 = _snapshot(app, "A active, after 5 more refreshes")
        _print(snap_a4)

        renders_after_pop = (RENDER_COUNTS["A"], RENDER_COUNTS["B"])
        a_render_delta_after_pop = renders_after_pop[0] - renders_at_pop[0]
        b_render_delta_after_pop = renders_after_pop[1] - renders_at_pop[1]
        print(
            f"  delta while A reactive: renders_A=+{a_render_delta_after_pop} "
            f"renders_B=+{b_render_delta_after_pop}"
        )

        # Pass/fail summary.
        print()
        print("=" * 78)
        print("Decision gate:")
        print("=" * 78)
        ok = True
        if a_render_delta != 0:
            print(f"  FAIL: A's widgets re-rendered {a_render_delta} times while B was active.")
            print("        Expected 0 — compositor IS walking suspended screens.")
            ok = False
        else:
            print("  PASS: A's widgets received zero new render() calls while B was active.")

        if b_render_delta_after_pop != 0:
            print(
                f"  FAIL: B's widgets re-rendered {b_render_delta_after_pop} times after A reactivated."
            )
            print("        Expected 0 — compositor IS walking suspended screens.")
            ok = False
        else:
            print("  PASS: B's widgets received zero new render() calls after A reactivated.")

        # Compositor size check: while B active, compositor count should
        # correspond to B's tree only.
        b_comp = int(snap_b2["compositor_full_map_size"] or 0)  # type: ignore[arg-type]
        a_comp = int(snap_a2["compositor_full_map_size"] or 0)  # type: ignore[arg-type]
        if b_comp > a_comp + 50:
            print(f"  FAIL: compositor full_map size grew while B active ({a_comp} -> {b_comp}).")
            ok = False
        else:
            print(
                f"  PASS: compositor full_map sized for active screen only "
                f"(A: {a_comp}, B: {b_comp})."
            )

        print()
        print(
            "RESULT:",
            "PASS — P1 viable (Stage 3 unlocked)."
            if ok
            else "FAIL — P1 dead. Re-route to Stage 4 or 5 after Stages 1 & 2.",
        )
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
