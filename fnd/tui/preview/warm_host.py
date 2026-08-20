"""Build and capture chunks on a screen the user never sees.

Warming needs somewhere to build a chunk widget so it can be captured. Doing
that in the on-screen container is what made warming compete with navigation,
and hiding that container is not an option either:

* ``display: none`` zeroes the layout, so ``freeze`` correctly refuses — a
  capture needs real geometry;
* ``opacity: 0`` keeps the geometry but blends every foreground into the
  background. Measured, the capture carries all 241 characters with fg == bg on
  every inked segment: content present, invisible. Worse than a refusal, because
  nothing detects it and the blank document gets cached and served;
* an off-viewport ``offset`` captures correctly but stays in layout flow, so it
  still costs the arrange time freezing exists to remove, and an ``auto``-height
  sibling squeezes the ``1fr`` document view to nothing.

A screen that is installed but not current has none of those problems. Textual's
compositor walks the active screen only, so a suspended screen costs nothing per
tick, while its widgets stay alive and laid out. Measured against a visible
capture: identical palette, correct ink, and an arbitrary capture width —
36/60/96 columns for 40/64/100 requested — with the live screen untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.screen import Screen

from fnd.matching import MatchSpec
from fnd.tui.preview import tuning
from fnd.tui.preview.frozen import FrozenChunk, freeze
from fnd.tui.widgets.markdown import FNDMarkdown, _legacy_blocks_to_md

if TYPE_CHECKING:
    from fnd.query import FileChunk
    from fnd.tui.app import FNDApp

__all__ = ["WarmHost"]

# Height handed to the off-screen layout pass. Only an upper bound: freeze
# captures at max(size.height, virtual_size.height), so a taller chunk is still
# captured whole — this just has to be generous enough that the layout does not
# constrain a normal chunk.
_LAYOUT_HEIGHT = 400

_SCREEN_NAME = "_fnd_warm"


class WarmHost:
    """A private screen used only to build chunks and capture them."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        self._screen: Screen[None] | None = None
        self._container: VerticalScroll | None = None

    async def ensure(self) -> VerticalScroll | None:
        """The off-screen container, created on first use. ``None`` if the app
        is being torn down."""
        if self._container is not None and self._container.is_mounted:
            return self._container
        try:
            # Reuse an already-installed screen. The name is fixed, so a retry
            # after a partial failure (install succeeded, `mount` below raised)
            # would otherwise raise on the duplicate name, be swallowed by the
            # `except`, and disable warming for the rest of the session — with
            # no error anywhere: captures simply stop and every lookup misses.
            screen = self._screen
            if screen is None or not self._app.is_screen_installed(_SCREEN_NAME):
                screen = Screen(id=_SCREEN_NAME)
                self._app.install_screen(screen, name=_SCREEN_NAME)
                self._screen = screen
                # A screen must be MOUNTED before anything can be mounted into
                # it, and only a push mounts it. Popping leaves it installed and
                # alive but not current, which is the state we want.
                #
                # The push is NOT awaited, and the pop follows immediately in
                # the same tick. Awaiting it lets this empty screen become
                # current and PAINT — measured live, the whole app went blank
                # for 35 consecutive frames (~1.75s) the first time warming ran,
                # sidebar included.
                self._app.push_screen(_SCREEN_NAME)
                self._app.pop_screen()
            container = VerticalScroll()
            # No scrollbar. This container is a measuring jig, not something
            # anyone scrolls: a chunk taller than `_LAYOUT_HEIGHT` overflows it,
            # it grows a bar, and the chunk then lays out a column narrower than
            # the width asked for — so the capture is cut for one width and
            # filed under another. Measured, the boundary is exactly the layout
            # box: 300 rendered rows captured at 76, 420 rows at 75.
            container.styles.scrollbar_size_vertical = 0
            await screen.mount(container)
        except Exception:
            return None
        self._screen = screen
        self._container = container
        return container

    async def capture(
        self, chunk: FileChunk, width: int, *, match_spec: MatchSpec | None = None
    ) -> FrozenChunk | None:
        """Build ``chunk`` off-screen at ``width`` and capture it.

        The widget is constructed exactly as the on-screen path constructs it,
        because the capture IS that widget's own output — any divergence would
        show up as a preview differing from the widget path's.

        ``match_spec`` should be the caller's SNAPSHOT, not the app's live one.
        Warming runs for seconds across many chunks; reading the live spec meant
        a query change mid-batch produced chunks highlighted for the new query
        and filed under the old query's key, where nothing can ever correct them.
        """
        container = await self.ensure()
        if container is None or self._screen is None:
            return None
        widget = FNDMarkdown(
            chunk.body_md or _legacy_blocks_to_md(chunk.blocks),
            match_spec=match_spec if match_spec is not None else self._app._effective_match_spec,
            render_mermaid=(
                self._app._config.defaults.render_mermaid if self._app._config else True
            ),
            classes="chunk-section chunk-md-body chunk-first",
        )
        try:
            await container.mount(widget)
            # Bounded. WarmHost is serial, so a build that never completes stops
            # the whole coverage pipeline for the session with nothing logged.
            async with asyncio.timeout(tuning.WARM_BUILD_TIMEOUT):
                await widget.build_done.wait()
            # The screen is not current, so Textual will not lay it out on its
            # own — Screen._on_timer_update gates relayout on is_current. Drive
            # it explicitly at the width the preview pane will paint at.
            #
            # TWICE, with a yield to the message pump in between, and the yield
            # is the part that matters. A DataTable sizes itself in response to
            # its own posted refresh, so after a single pass it holds its rows
            # with NO geometry at all — measured rows=3, size=0, virtual=0 — and
            # the capture comes back with the table's border and none of its
            # contents. That is the "tables stopped being formatted" report:
            # an empty box where the table was. A second pass alone does not fix
            # it (still 0); a pump between them does (size=4, all cells present).
            self._screen._refresh_layout(Size(width, _LAYOUT_HEIGHT))
            await asyncio.sleep(0)
            self._screen._refresh_layout(Size(width, _LAYOUT_HEIGHT))
            return freeze(widget, chunk.chunk_seq)
        except TimeoutError:
            with contextlib.suppress(Exception):
                self._app._diag_log(f"warm build timed out chunk={chunk.chunk_seq}")
            return None
        except Exception:
            return None
        finally:
            # NOT awaited, and suppressing BaseException rather than Exception.
            # Cancellation is the normal way warming ends, CancelledError is a
            # BaseException, and it lands ON the await — so an awaited removal
            # here is skipped exactly when it is needed. Measured: 12 whole
            # widget trees (~28 widgets each) stranded across 29 cancellations,
            # unbounded over a session and invisible to the row budget.
            # ``remove()`` posts the removal and does not need awaiting.
            with contextlib.suppress(BaseException):
                widget.remove()

    # Deliberately no dispose(). One screen holding one empty container lives
    # for the session; each captured widget is removed in the finally above, so
    # nothing accumulates, and process exit reclaims the rest. An uncalled
    # teardown method would only promise a cleanup nobody performs.
