"""Serving a captured document instead of rebuilding the widget tree.

The freeze sweep already captures every chunk the user reads; those captures
used to die with the container. Keeping them makes a second visit to a file a
scroll rather than a rebuild — no build wait, no settle barrier, no retry chain,
which is where the measured navigation latency lives.

These tests pin the two things that make that safe: the served document is a
CONTIGUOUS run (a hole silently shifts every row after it), and navigating to it
really does bypass the rebuild rather than merely looking fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.index import build_index
from fnd.tui import FNDApp
from fnd.tui.preview.frozen import FrozenDocumentView
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
async def test_warming_extends_the_run_to_cover_the_whole_file(
    tmp_path: Path, tmp_index_dir: Path, doc_preview: None
) -> None:
    """A captured run stops short; warming must finish the job.

    The background fill bails the moment the user takes scroll control, so the
    tail is missing and a jump there falls back to a rebuild — the slow path the
    substrate exists to avoid. Acceptance is COVERAGE (which jumps can be served
    instantly), not latency, because latency on this branch has been misleading
    three separate times.

    Growth must also stay contiguous: the assertion is on the exact chunk
    sequence, so a skipped chunk fails here rather than silently shifting every
    row after it.
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
        searcher = app._search.searcher
        assert searcher is not None
        all_seqs = [c.chunk_seq for c in searcher.get_file_chunks(group.parent_id)]

        await wait_until(
            pilot,
            lambda: app._preview.document_store.get(group.parent_id, sig, width) is not None,
            timeout=20.0,
            message="nothing was harvested",
        )
        before = app._preview.document_store.get(group.parent_id, sig, width)
        assert before is not None
        assert len(before.chunks) < len(all_seqs), (
            "fixture captured the whole file first time — warming has nothing to prove here"
        )

        await wait_until(
            pilot,
            lambda: (
                (d := app._preview.document_store.get(group.parent_id, sig, width)) is not None
                and len(d.chunks) == len(all_seqs)
            ),
            timeout=25.0,
            message="warming never covered the whole file",
        )
        doc = app._preview.document_store.get(group.parent_id, sig, width)
        assert doc is not None
        assert [c.chunk_seq for c in doc.chunks] == all_seqs, (
            "warmed document is not the file in order — a hole shifts every row after it"
        )
        assert doc.total_rows == sum(c.height for c in doc.chunks)
        # Every chunk is now instantly jumpable, which is the point.
        assert all(doc.row_of_chunk(seq) is not None for seq in all_seqs)


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
