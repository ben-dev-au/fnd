"""Warmth end to end: coverage's store, the poll, and the arrows.

The unit tests pin the classification and the glyph. What they cannot see
is the wiring — that the presenter can resolve a width and a query
signature to ask the store anything at all, that the poll reaches the
tree, and that the progress line picks its plan from the same fact the
arrow paints. All of that only exists in a running app.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
from rich.style import Style
from textual.containers import VerticalScroll

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview.warmth import WarmState
from fnd.tui.widgets.results_tree import ResultsTree
from tests._pilot_wait import run_search, wait_until


@pytest.fixture
def warm_index(tmp_path: Path, tmp_index_dir: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    for name in ("alpha", "beta"):
        (root / f"{name}.md").write_text(
            "\n".join(
                textwrap.dedent(f"""
                ## {name} section {i}

                target paragraph {i} with enough words to make a real chunk.
                """)
                for i in range(12)
            ),
            encoding="utf-8",
        )
    build_index(roots=[root], index_dir=tmp_index_dir, collection="default")
    return tmp_index_dir


def hold_everything(app: FNDApp) -> None:
    """Report every chunk as held, without building real captures.

    Patches ``has``, which is what warmth probes with — ``get`` promotes on
    read and warmth must never do that. Keeping the fake off the warm host
    also keeps these tests off a serial resource that captures at ~10 chunks
    a second.
    """
    app._preview.capture_store.has = lambda *a, **k: True  # type: ignore[assignment]


def hold_nothing(app: FNDApp) -> None:
    app._preview.capture_store.has = lambda *a, **k: False  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_readiness_is_answerable_in_a_live_app(warm_index: Path) -> None:
    """Needs a laid-out pane (for the capture width) and a committed query
    (for the signature). Before either, the map is empty rather than wrong —
    reporting the whole list cold would be a lie, not a default."""
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")

        hold_nothing(app)
        states = app._preview.warm_states()
        assert states, "warmth could not be answered at all in a running app"
        # Nothing is READY. Not "everything is COLD": real coverage is running
        # underneath, so whichever file it is capturing right now legitimately
        # reads WARMING, and pinning that would be a timing-dependent test.
        assert WarmState.READY not in set(states.values())

        hold_everything(app)
        warm = app._preview.warm_states()
        assert warm is not None
        assert set(warm.values()) == {WarmState.READY}


@pytest.mark.asyncio
async def test_the_file_being_captured_reads_as_warming(warm_index: Path) -> None:
    """Captures run serially on one off-screen host, so exactly one file is
    ever in this state — which is what makes it a single marker walking
    outward from the cursor rather than noise across the list."""
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        hold_nothing(app)

        target = app._search.groups[0].parent_id
        app._preview.coverage_parent = target
        states = app._preview.warm_states()
        assert states is not None

        assert states[target] is WarmState.WARMING
        others = [s for pid, s in states.items() if pid != target]
        assert WarmState.WARMING not in others, "more than one file claimed to be warming"


@pytest.mark.asyncio
async def test_the_poll_repaints_the_arrows(warm_index: Path) -> None:
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        tree = app.query_one("#results_pane", ResultsTree)

        hold_nothing(app)
        app._results.refresh_warmth()
        assert set(tree.warm_states.values()) == {WarmState.COLD}

        hold_everything(app)
        assert app._results.refresh_warmth() is True
        assert set(tree.warm_states.values()) == {WarmState.READY}


@pytest.mark.asyncio
async def test_the_timer_drives_the_poll_without_being_asked(warm_index: Path) -> None:
    """The wiring test. Warmth changes with no user input at all — a capture
    landing, or coverage stepping to the next file — so nothing in the app's
    normal event flow would repaint it."""
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        tree = app.query_one("#results_pane", ResultsTree)
        hold_everything(app)
        tree.warm_states = {}

        await wait_until(
            pilot,
            lambda: bool(tree.warm_states),
            timeout=10.0,
            message="nothing ever polled warmth onto the results tree",
        )
        assert set(tree.warm_states.values()) == {WarmState.READY}


@pytest.mark.asyncio
async def test_a_ready_file_is_priced_as_a_warm_navigation(warm_index: Path) -> None:
    """The progress line and the arrow must read the same fact. A file whose
    hits are all captured mounts by blitting them, so pricing it with a cold
    plan would overstate every jump into it — and the chunk cache, which the
    classification used to ask, cannot see captures at all."""
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        # A file the pane is NOT showing, so only warmth can make it warm.
        other = next(
            g.parent_id for g in app._search.groups if g.parent_id != app._preview.showing_parent()
        )
        app._preview.chunk_cache.pop(other, None)

        hold_nothing(app)
        cold_plan = app._nav_progress.plan_for(other)

        hold_everything(app)
        warm_plan = app._nav_progress.plan_for(other)

        assert cold_plan is not warm_plan, "captures made no difference to the plan"
        assert "cold" in cold_plan.operation_id
        assert "warm" in warm_plan.operation_id


@pytest.mark.asyncio
async def test_a_new_query_does_not_inherit_the_old_arrows(warm_index: Path) -> None:
    """A new query clears the capture store. Warmth is keyed by parent_id, so
    a file listed by both searches would otherwise keep its READY arrow until
    the next poll — promising an instant jump whose captures had just been
    thrown away."""
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        tree = app.query_one("#results_pane", ResultsTree)
        hold_everything(app)
        app._results.refresh_warmth()
        assert set(tree.warm_states.values()) == {WarmState.READY}, "setup"

        hold_nothing(app)
        await run_search(pilot, app, "paragraph")

        stale = [p for p, s in tree.warm_states.items() if s is WarmState.READY]
        assert not stale, "rows kept a READY arrow across a query that cleared the store"


def test_probing_warmth_does_not_reorder_the_capture_store() -> None:
    """The store promotes on READ so the file being read is never the oldest
    entry — a cache that drops what you are looking at is worse than no cache.

    Warmth probes every listed file twice a second. Asking through ``get``
    re-promoted all of them in results-list order on every tick, which
    neutralised that protection and left the top result — usually the file on
    screen — as the first eviction victim. Probing is not use.
    """
    from fnd.tui.preview.frozen_store import ChunkCaptureStore

    store = ChunkCaptureStore()
    for name in ("onscreen", "b", "c"):
        store._files[(name, "sig", 80)] = {0: object()}  # type: ignore[assignment]

    # Serving the on-screen file promotes it, as designed.
    store.get("onscreen", "sig", 80, 0)
    assert list(store._files)[-1][0] == "onscreen"

    # A warmth poll walks every file in results-list order.
    for name in ("onscreen", "b", "c"):
        store.has(name, "sig", 80, 0)

    assert list(store._files)[-1][0] == "onscreen", (
        "probing warmth reordered the store and undid the read-promotion"
    )


@pytest.mark.asyncio
async def test_every_state_is_actually_coloured(warm_index: Path) -> None:
    """Each state must CARRY a colour, not merely differ from the others.

    The first version of this check only asserted that cold and warm differed,
    and it passed while the bug was live: warm inherited the stock icon style
    and had no colour at all, so the filled arrow rendered plain white. "They
    differ" was satisfied by one of them being unstyled.

    Component styles only resolve inside a running app with the stylesheet
    applied, which is why this cannot live beside the glyph tests.
    """
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        tree = app.query_one("#results_pane", ResultsTree)
        node = tree.root.children[0]
        parent_id = node.data["group"].parent_id  # type: ignore[index]

        # A distinctive base so "inherited the row's colour" is unmistakable.
        # Comparing against a literal "white" string does NOT work: str(Color)
        # is the full repr, which is how the first version of this test passed
        # while every warm arrow was rendering plain white.
        base = Style(color="magenta")
        inherited = base.color

        seen: dict[WarmState, object] = {}
        for state in WarmState:
            tree.apply_warm_states({parent_id: state})
            span = tree.render_label(node, base, Style()).spans[0]
            assert isinstance(span.style, Style)
            colour = span.style.color
            assert colour is not None, f"{state.name} arrow has no colour of its own"
            assert colour != inherited, (
                f"{state.name} arrow inherited the row's colour instead of its own"
            )
            seen[state] = colour

        assert seen[WarmState.COLD] != seen[WarmState.READY], "cold and ready look the same"
        assert seen[WarmState.WARMING] == seen[WarmState.READY], (
            "warming and ready are both warm — they differ by glyph, not hue"
        )


@pytest.mark.asyncio
async def test_the_arrow_colours_follow_the_theme(warm_index: Path) -> None:
    """Read from component classes rather than baked-in hex, so a theme change
    moves them. Cold is the score column's blue; warm is the theme accent —
    the same variable the progress line's fill uses."""
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", ResultsTree)
        warm = tree.get_component_rich_style("results--warm").color
        cold = tree.get_component_rich_style("results--cold").color
        assert warm is not None
        assert cold is not None
        assert warm.triplet is not None
        assert cold.triplet is not None
        # Warm is the warm end of the spectrum, cold the cool end. Cheap, but
        # it is the property the whole scheme rests on.
        assert warm.triplet.red > warm.triplet.blue, "the warm arrow is not warm"
        assert cold.triplet.blue > cold.triplet.red, "the cold arrow is not cold"


@pytest.mark.asyncio
async def test_warming_survives_the_gap_between_captures(warm_index: Path) -> None:
    """``coverage_parent`` is what the arrows read as "being warmed now".

    It used to be set around each individual capture and cleared in that
    call's ``finally``. A capture is ~60 ms and is followed by a yield at
    least as long, so the flag was unset more often than set, a poll twice a
    second rarely caught it, and files appeared to go COLD straight to READY.

    Sampled CONCURRENTLY, because the gap is what matters and it is invisible
    from inside a capture: under the old code the flag was set on entry to
    every capture and cleared the moment each one returned. Only an observer
    running between them — which is exactly what the 2 Hz poll is — can tell
    the two arrangements apart.
    """
    app = FNDApp(index_dir=warm_index)
    async with app.run_test() as pilot:
        await pilot.pause()
        await run_search(pilot, app, "target")
        preview = app._preview
        parent_id = app._search.groups[0].parent_id
        chunks = await preview._coverage_chunks(parent_id)
        assert len(chunks) >= 2, "setup — need at least two chunks to capture"

        async def slow_capture(*_a: object, **_k: object) -> None:
            # Long enough that the inter-capture backoff yields a real window.
            await asyncio.sleep(0.03)
            return None

        preview._warm_host.capture = slow_capture  # type: ignore[assignment]

        observed: list[str | None] = []
        stop = asyncio.Event()

        async def sampler() -> None:
            while not stop.is_set():
                observed.append(preview.coverage_parent)
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(sampler())
        try:
            await preview._capture_targets(
                parent_id,
                app._search.query_signature(),
                preview.capture_width(app.query_one("#preview_pane", VerticalScroll)),
                chunks,
                [0, 1],
                app._effective_match_spec,
                lambda: True,
            )
        finally:
            stop.set()
            await watcher

        assert observed, "setup — the sampler never ran"
        assert None not in observed, (
            "the warming marker was dropped between captures — the arrows would "
            f"flicker back to cold mid-file: {observed}"
        )
        assert preview.coverage_parent is None, "the marker outlived the file"
