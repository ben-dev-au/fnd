"""Serving a captured document instead of rebuilding the widget tree.

The freeze sweep already captures every chunk the user reads; those captures
used to die with the container. Keeping them makes a second visit to a file a
scroll rather than a rebuild — no build wait, no settle barrier, no retry chain,
which is where the measured navigation latency lives.

These tests pin the two things that make that safe: the served document is a
CONTIGUOUS run (a hole silently shifts every row after it), and navigating to it
really does bypass the rebuild rather than merely looking fast.

Known limit, discovered by measurement rather than reasoning: warming can only
run while a laid-out container exists, because capture needs real geometry.
Serving a document hides the container (``display: none``), which zeroes layout
and makes ``freeze`` refuse — so warming cannot run in the state where it would
help most, and upward growth is effectively unreachable. Coverage therefore
grows downward during widget-path visits only.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import ClassVar

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview.frozen import FrozenDocument, FrozenDocumentView
from tests._pilot_wait import settle, wait_until


@pytest.fixture
def doc_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_FND_DOC_PREVIEW", "1")


def _corpus(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """Two markdown files, each under the full-mount budget (so most of the file
    is captured) and each carrying several spread-out matches."""
    notes = tmp_path / "notes"
    notes.mkdir()
    for name in ("alpha", "beta"):
        lines: list[str] = [f"# {name}", ""]
        for section in range(40):
            lines.append(f"## {name} section {section}")
            lines.append(
                f"quartzfin marker in {name} section {section}."
                if section % 5 == 0
                else f"Filler prose for {name} section {section}."
            )
            lines.extend([f"More filler {i} in section {section}." for i in range(3)])
            lines.append("")
        (notes / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir


@pytest.mark.asyncio
async def test_a_captured_document_is_a_contiguous_run(
    tmp_path: Path, tmp_index_dir: Path, doc_preview: None
) -> None:
    """A stored document must have no holes.

    Row positions accumulate chunk heights from the top, so one missing chunk in
    the middle shifts every match after it — silently, since nothing raises.
    Completeness is NOT the rule (the background fill stops when the user takes
    scroll control, so a 41-chunk file typically captures 40); contiguity is,
    because a run is self-consistent for every chunk inside it.
    """
    index = _corpus(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        group = app._search.groups[0]
        await wait_until(
            pilot,
            lambda: (
                app._preview.document_store.get(
                    group.parent_id,
                    app._search.query_signature(),
                    app.query_one("#preview_pane").content_size.width,
                )
                is not None
            ),
            timeout=20.0,
            message="no document was ever harvested",
        )
        searcher = app._search.searcher
        assert searcher is not None
        chunks = searcher.get_file_chunks(group.parent_id)
        doc = app._preview.document_store.get(
            group.parent_id,
            app._search.query_signature(),
            app.query_one("#preview_pane").content_size.width,
        )
        assert doc is not None
        seqs = [c.chunk_seq for c in doc.chunks]
        assert len(seqs) > 1, f"captured almost nothing ({seqs})"
        order = [c.chunk_seq for c in chunks]
        start = order.index(seqs[0])
        assert seqs == order[start : start + len(seqs)], (
            f"captured chunks {seqs} are not a contiguous run of {order} — a hole "
            "shifts every row after it"
        )
        # Row bookkeeping must agree with the strips actually held.
        assert doc.total_rows == sum(c.height for c in doc.chunks)
        assert doc.starts == [sum(c.height for c in doc.chunks[:i]) for i in range(len(doc.chunks))]


@pytest.mark.asyncio
async def test_serving_a_partial_document_starts_warming(
    tmp_path: Path, tmp_index_dir: Path, doc_preview: None
) -> None:
    """A file served from the store must still be warmed towards completeness.

    Warming is started by the harvest that follows a widget-path mount, and the
    document path returns BEFORE harvest — so a store-served file could only
    grow if it had once been built the slow way, which meant warming helped the
    first file of a session and no other.

    Exercised at the seam rather than end-to-end: any file small enough for a
    fast test warms to completion before a revisit can be staged, so an
    end-to-end version silently tests nothing (an earlier one passed without the
    fix for exactly that reason).
    """
    index = _corpus(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        group = app._search.groups[0]
        sig = app._search.query_signature()
        width = app.query_one("#preview_pane").content_size.width
        await wait_until(
            pilot,
            lambda: app._preview.document_store.get(group.parent_id, sig, width) is not None,
            timeout=20.0,
            message="nothing was harvested",
        )
        full = app._preview.document_store.get(group.parent_id, sig, width)
        assert full is not None

        partial = FrozenDocument()
        for chunk in full.chunks[: max(2, len(full.chunks) // 2)]:
            partial.append(chunk)

        # Quiesce any warm already in flight, so the guard cannot mask the call.
        task = app._preview._warm_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        app._preview._warm_parent = None

        app._preview._warm_served_document(group.parent_id, partial, width)
        await wait_until(
            pilot,
            lambda: app._preview._warm_parent == group.parent_id,
            timeout=20.0,
            message="serving a partial document did not start warming",
        )


def test_the_store_is_bounded_by_rows_not_by_document_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache must bound what actually grows.

    MAX_DOCUMENTS was the only cap while a document held a handful of chunks.
    Warming grows one to the whole file, and a captured chunk measures 44.5 KB
    on the real corpus (1670 bytes per row), so four whole-file documents is
    254 MB — a count-based cap guarding the wrong quantity.

    The document just stored is never evicted: it is the one on screen, so
    dropping it would rebuild what the user is reading. A single oversized file
    is therefore allowed to exceed the budget; the budget bounds the CACHE.
    """
    from fnd.tui.preview import frozen_store
    from fnd.tui.preview.frozen_store import FrozenDocumentStore
    from tests.test_preview_frozen_document import _chunk  # reuse the strip builder

    # Patched, not measured: the real budget scales with system RAM, so a test
    # that allocated against it would build millions of Strips on a big machine
    # and prove something different on every developer's laptop.
    budget = 300
    monkeypatch.setattr(frozen_store, "budget_rows", lambda: budget)
    store = FrozenDocumentStore()
    rows_each = budget // 3
    for i in range(6):
        doc = FrozenDocument()
        doc.append(_chunk(i, rows_each))
        store.put(f"file{i}", "sig", 40, doc)
        assert store.total_rows() <= budget or len(store._docs) == 1, (
            f"store holds {store.total_rows()} rows across {len(store._docs)} "
            f"documents, over the {budget} budget"
        )
    # The most recent file must still be served — it is what is on screen.
    assert store.get("file5", "sig", 40) is not None, "evicted the document in use"
    # And the oldest must be gone rather than accumulating.
    assert store.get("file0", "sig", 40) is None, "nothing was evicted under pressure"


@pytest.mark.asyncio
async def test_a_new_query_stops_warming_and_drops_its_captures(
    tmp_path: Path, tmp_index_dir: Path, doc_preview: None
) -> None:
    """Captures carry the OLD query's highlighting.

    They can never be served after the query changes — the store key carries the
    query signature — so warming that continues is building results nothing can
    read, while holding row budget the new query's captures need. Both the task
    and the cache must go on the reset, not at the next batch boundary.
    """
    index = _corpus(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        await wait_until(
            pilot,
            lambda: len(app._preview.document_store._docs) > 0,
            timeout=20.0,
            message="nothing was harvested",
        )
        app._preview.bump_reset_generation()
        await settle(pilot, ticks=4)

        assert not app._preview.document_store._docs, (
            "captures for the superseded query survived — they can never be "
            "served and they spend the row budget"
        )
        task = app._preview._warm_task
        assert task is None or task.done() or task.cancelling() > 0, (
            "warming continued after the query changed, building captures whose "
            "highlighting is already stale"
        )


def test_a_single_oversized_document_is_kept_rather_than_thrashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One file bigger than the whole budget must still be served.

    Evicting it would mean the user's current file is rebuilt on every
    navigation — strictly worse than the memory it costs.
    """
    from fnd.tui.preview import frozen_store
    from fnd.tui.preview.frozen_store import FrozenDocumentStore
    from tests.test_preview_frozen_document import _chunk

    budget = 300
    monkeypatch.setattr(frozen_store, "budget_rows", lambda: budget)
    store = FrozenDocumentStore()
    doc = FrozenDocument()
    doc.append(_chunk(0, budget * 2))
    store.put("huge", "sig", 40, doc)
    assert store.get("huge", "sig", 40) is not None


@pytest.mark.asyncio
async def test_cancelling_a_capture_strands_no_widgets() -> None:
    """Cancellation is how warming normally ends, so cleanup must survive it.

    ``CancelledError`` is a BaseException and it lands ON the await, so an
    awaited removal inside ``finally`` is skipped exactly when it is needed —
    measured at 12 stranded widget trees (~28 widgets each) across 29
    cancellations, unbounded over a session and invisible to the row budget
    because the strands are widgets, not rows.
    """
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll

    from fnd.matching import MatchSpec
    from fnd.tui.preview.warm_host import WarmHost

    body = "## quartzfin heading\n\n" + "\n\n".join(f"Paragraph {i}." for i in range(6))

    class _Chunk:
        chunk_seq = 1
        body_md = body
        blocks: ClassVar[list[object]] = []

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="pane")

    app = _Host()
    app._effective_match_spec = MatchSpec.from_query("quartzfin")  # type: ignore[attr-defined]
    app._config = None  # type: ignore[attr-defined]
    async with app.run_test(size=(100, 30)) as pilot:
        host = WarmHost(app)  # type: ignore[arg-type]
        # Prime the screen so the cancellations below land inside capture().
        await host.capture(_Chunk(), width=60)  # type: ignore[arg-type]
        container = host._container
        assert container is not None

        for turns in range(1, 24):
            task = asyncio.ensure_future(host.capture(_Chunk(), width=60))  # type: ignore[arg-type]
            for _ in range(turns):
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for _ in range(8):
            await pilot.pause()

        assert len(container.children) == 0, (
            f"{len(container.children)} widget trees stranded on the warm screen "
            "after cancelled captures — each is a whole chunk's tree"
        )


@pytest.mark.asyncio
async def test_off_screen_capture_is_visible_and_matches_the_on_screen_one() -> None:
    """A capture built off-screen must be INK, not invisible ink.

    Checked on colour, not glyphs. An earlier attempt hid the container with
    opacity:0 and compared ``strip.text``: every character was present and every
    one had foreground == background, so the capture was blank and nothing
    detected it. Comparing the palette against a visible capture is the check
    that would have caught it.
    """
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll

    from fnd.matching import MatchSpec
    from fnd.tui.preview.frozen import freeze
    from fnd.tui.preview.warm_host import WarmHost
    from fnd.tui.widgets.markdown import FNDMarkdown

    body = (
        "## Heading with quartzfin\n\nA paragraph mentioning quartzfin in prose.\n\n"
        + "\n\n".join(f"Filler paragraph {i} with text." for i in range(6))
    )

    def palette(cap) -> tuple[int, int, set[tuple[str, str]]]:  # type: ignore[no-untyped-def]
        inked = invisible = 0
        seen: set[tuple[str, str]] = set()
        for strip in cap.strips:
            for seg in strip:
                if not seg.text.strip() or seg.style is None:
                    continue
                inked += 1
                fg, bg = str(seg.style.color), str(seg.style.bgcolor)
                seen.add((fg, bg))
                if fg == bg:
                    invisible += 1
        return inked, invisible, seen

    class _Host(App[None]):
        def compose(self) -> ComposeResult:
            with VerticalScroll(id="pane"):
                yield FNDMarkdown(match_spec=MatchSpec.from_query("quartzfin"), id="md")

    app = _Host()
    app._effective_match_spec = MatchSpec.from_query("quartzfin")  # type: ignore[attr-defined]
    app._config = None  # type: ignore[attr-defined]
    async with app.run_test(size=(100, 30)) as pilot:
        live = app.query_one("#md", FNDMarkdown)
        live.update(body)
        await live.build_done.wait()
        for _ in range(12):
            await pilot.pause()
        control = freeze(live, chunk_seq=0)
        assert control is not None
        _, control_invisible, control_palette = palette(control)
        assert control_invisible == 0, "control capture was already invisible"

        class _Chunk:
            chunk_seq = 1
            body_md = body
            blocks: ClassVar[list[object]] = []

        host = WarmHost(app)  # type: ignore[arg-type]
        captured = await host.capture(_Chunk(), width=live.size.width)  # type: ignore[arg-type]
        assert captured is not None, "off-screen capture was refused"
        inked, invisible, pal = palette(captured)
        assert inked > 0, "off-screen capture had no inked segments"
        assert invisible == 0, (
            f"{invisible} of {inked} inked segments have foreground == background — "
            "the capture is present but invisible, and would cache as a blank preview"
        )
        assert pal == control_palette, (
            f"off-screen palette {sorted(pal)} differs from the visible one "
            f"{sorted(control_palette)} — the capture would not match the widget path"
        )
        assert app.screen is not host._screen, "the warm screen became current"


@pytest.mark.asyncio
async def test_a_width_change_invalidates_captures_but_a_height_change_does_not(
    tmp_path: Path, tmp_index_dir: Path, doc_preview: None
) -> None:
    """Strips are cut for one width and cannot be re-wrapped.

    Serving them after a horizontal resize paints a document at the wrong width
    — text clipped or trailing blanks — so a width change must invalidate. A
    HEIGHT change must not: every capture is still valid, and dropping them
    would turn a window drag into a rebuild of everything already read.
    """
    index = _corpus(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: bool(app._search.groups) and app._preview.active is not None,
            timeout=20.0,
            message="preview never became active",
        )
        await wait_until(
            pilot,
            lambda: len(app._preview.document_store._docs) > 0,
            timeout=20.0,
            message="nothing was harvested",
        )
        group = app._search.groups[0]
        sig = app._search.query_signature()
        captured_width = app.query_one("#preview_pane").content_size.width
        store = app._preview.document_store
        assert store.get(group.parent_id, sig, captured_width) is not None

        # Height only: the capture is still served.
        await pilot.resize_terminal(100, 40)
        await settle(pilot, ticks=6)
        width = app.query_one("#preview_pane").content_size.width
        assert width == captured_width, "fixture error: the height change moved the width too"
        assert store.get(group.parent_id, sig, width) is not None, (
            "a height-only resize stopped the capture being served — a window drag "
            "would rebuild everything the user had read"
        )

        # Width: nothing may be served for the new width until it is re-captured.
        await pilot.resize_terminal(140, 40)
        await settle(pilot, ticks=6)
        width = app.query_one("#preview_pane").content_size.width
        assert width != captured_width, (
            "fixture error: the terminal resize did not change the pane width"
        )
        assert store.get(group.parent_id, sig, width) is None, (
            "a capture cut for the old width is being served at the new one — it "
            "would paint clipped or short"
        )


@pytest.mark.asyncio
async def test_revisiting_a_file_serves_the_document_and_skips_the_rebuild(
    tmp_path: Path, tmp_index_dir: Path, doc_preview: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the substrate: no widget rebuild on the way back.

    Asserts the rebuild is NOT REACHED, not merely that the result looks right —
    a document that renders correctly while still rebuilding underneath would
    pass a visual check and deliver none of the latency win.
    """
    index = _corpus(tmp_path, tmp_index_dir)
    app = FNDApp(index_dir=index, initial_query="quartzfin")
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(
            pilot,
            lambda: len(app._search.groups) >= 2 and app._preview.active is not None,
            timeout=20.0,
            message="need two files in the results",
        )
        first, second = app._search.groups[0], app._search.groups[1]
        sig = app._search.query_signature()
        width = app.query_one("#preview_pane").content_size.width

        await wait_until(
            pilot,
            lambda: app._preview.document_store.get(first.parent_id, sig, width) is not None,
            timeout=20.0,
            message="first file never harvested",
        )

        # Navigate away, then back. The return trip must be served.
        app._preview.render_full_doc(second.parent_id, focus_chunk_seq=second.hits[0].chunk_seq)
        await settle(pilot, ticks=10)

        builds: list[str] = []
        original = type(app._preview)._mount_chunks_async

        def counting(self, parent_id, *args, **kwargs):  # type: ignore[no-untyped-def]
            builds.append(parent_id)
            return original(self, parent_id, *args, **kwargs)

        monkeypatch.setattr(type(app._preview), "_mount_chunks_async", counting)

        target = first.hits[-1].chunk_seq
        app._preview.render_full_doc(first.parent_id, focus_chunk_seq=target)
        await settle(pilot, ticks=10)

        view = app._preview.active_document_view()
        assert view is not None, "the captured document was not served"
        assert isinstance(view, FrozenDocumentView)
        assert first.parent_id not in builds, (
            f"the widget tree was rebuilt anyway (builds={builds}) — the document "
            "was served but the expensive path ran regardless"
        )
        # And it landed on the match, not merely somewhere in the file.
        row = view.document.match_row(target)
        assert row is not None
        top = int(view.scroll_offset.y)
        assert top <= row < top + view.size.height, (
            f"match row {row} outside viewport [{top}, {top + view.size.height})"
        )
