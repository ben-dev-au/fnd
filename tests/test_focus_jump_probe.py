# pyright: basic
"""Probe (interactive only): cursor behaviour during a real-PDF preview
load. Skipped from the default suite — touches a system-specific path
and takes minutes. Run manually with::

    uv run pytest tests/test_focus_jump_probe.py -v -s

The probe traces every cursor move on the results tree during the
mount task and prints a stack trace so we can see which code path is
moving the cursor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PDF_PATH = Path(
    "/Users/BenDavidson/Documents/Uni/B. Software Engineering (Honours)/"
    "2026 Semester 1/Cloud Platforms/9 - Resources/"
    "26S1CPL - wellarchitected-framework.pdf"
)


pytestmark = pytest.mark.skip(
    reason="manual probe — touches a system-specific PDF and runs for minutes"
)


@pytest.fixture
def real_pdf_corpus(tmp_path: Path) -> Path:
    """Drop the real wellarchitected-framework.pdf next to a synthetic
    second file so the results tree has multiple files to navigate."""
    if not _PDF_PATH.exists():
        pytest.skip(f"reference PDF missing: {_PDF_PATH}")
    root = tmp_path / "corpus"
    root.mkdir()
    # Copy the real PDF in by hard-link/copy so the index builder picks it up.
    import shutil

    shutil.copy(_PDF_PATH, root / _PDF_PATH.name)
    # A second small markdown file so the tree has a clear "first file"
    # to potentially jump to.
    (root / "decoy.md").write_text("# AWS Decoy\n\nThis is an AWS-flavoured decoy file.\n")
    return root


@pytest.mark.asyncio
async def test_cursor_stays_after_expand_during_real_pdf_load(
    real_pdf_corpus: Path, tmp_index_dir: Path
) -> None:
    """User-reported scenario, real-PDF reproduction. Build an index
    over the 1002-page wellarchitected-framework PDF + a decoy MD file,
    open the TUI with --query AWS, click into the PDF, then while it's
    mounting expand it in the tree. After the mount task completes the
    cursor must not bounce to the decoy / first file row."""
    import asyncio

    from textual.widgets import Tree

    from acorn.index import build_index
    from acorn.tui import AcornApp
    from acorn.tui.app import PreviewContainer

    build_index(roots=[real_pdf_corpus], index_dir=tmp_index_dir, collection="default")
    app = AcornApp(index_dir=tmp_index_dir, initial_query="AWS")
    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one("#results_pane", Tree)
        # Wait for the layered query to return groups for the PDF.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if len(tree.root.children) >= 1:
                break
        results = list(tree.root.children)
        assert results, "expected at least one result group"
        # Identify the PDF row.
        pdf_idx = next(
            (i for i, n in enumerate(results) if "wellarchitected" in str(n.label)),
            None,
        )
        assert (
            pdf_idx is not None
        ), f"PDF result not in tree; labels: {[str(n.label) for n in results]}"
        pdf_node = results[pdf_idx]
        # Instrument the tree's mutators so we see exactly which call
        # path moves the cursor. cursor_line is a Reactive (not a
        # property), so we wrap the methods that change it.
        import traceback

        cursor_changes: list[tuple[str, int, str]] = []

        def _trace(method_name: str, value: object) -> None:
            cursor_changes.append(
                (method_name, tree.cursor_line, "".join(traceback.format_stack(limit=15)))
            )

        # Wrap methods that move the cursor.
        for name in ("move_cursor", "action_cursor_down", "action_cursor_up"):
            original = getattr(tree, name, None)
            if original is None:
                continue

            def make_traced(orig, n):
                def wrapped(*args, **kwargs):
                    _trace(n, args)
                    return orig(*args, **kwargs)

                return wrapped

            setattr(tree, name, make_traced(original, name))

        # Also watch the reactive directly via a watch callback.
        orig_watch = tree.watch_cursor_line if hasattr(tree, "watch_cursor_line") else None

        def watcher(old: int, new: int) -> None:
            if old != new:
                cursor_changes.append(("watch", new, "".join(traceback.format_stack(limit=15))))
            if orig_watch:
                orig_watch(old, new)  # type: ignore[misc]

        tree.watch_cursor_line = watcher  # type: ignore[method-assign]

        # Move cursor to the PDF row and trigger a load.
        tree.cursor_line = pdf_node.line
        await asyncio.sleep(0.05)
        before_load = tree.cursor_line
        cursor_changes.clear()  # baseline: only count moves AFTER the load starts
        tree.post_message(Tree.NodeSelected(pdf_node))
        # Mid-load: brief pause to let the mount task start, then expand.
        await asyncio.sleep(0.05)
        pdf_node.expand()
        mid_cursor = tree.cursor_line
        # Drain the mount task with bounded wall-clock — for a real PDF
        # the full mount can take minutes; we just need to see the
        # post-completion cursor state.
        cursor_trace: list[int] = [before_load, mid_cursor]
        import time

        deadline = time.monotonic() + 240.0  # cap at 4 minutes
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            cursor_trace.append(tree.cursor_line)
            ap = app._active_preview
            if ap is not None and isinstance(ap, PreviewContainer) and ap.is_complete:
                for _ in range(10):
                    await asyncio.sleep(0.05)
                    cursor_trace.append(tree.cursor_line)
                break
        print(f"\nBefore: line {before_load} ({pdf_node.label})")
        print(f"Mid-load: line {mid_cursor}")
        print(f"After mount: line {tree.cursor_line}")
        print(f"Trajectory length: {len(cursor_trace)}; tail: {cursor_trace[-20:]}")
        # Identify what node the final cursor line points at.
        try:
            cur_node = tree.get_node_at_line(tree.cursor_line)
            print(f"Final cursor node: {cur_node.label if cur_node else None}")
            print(
                f"  parent: {cur_node.parent.label if cur_node and cur_node.parent and cur_node.parent is not tree.root else 'ROOT'}"
            )
        except Exception as e:
            print(f"could not introspect cursor node: {e}")
        # Find first index in trace where cursor moved away from before_load
        for i, line in enumerate(cursor_trace):
            if line != before_load:
                print(f"First move at sample {i}: {before_load} -> {line}")
                break
        # Print every cursor change with a stack trace so we can see
        # exactly which code path moved the cursor.
        print(f"\nCursor changed {len(cursor_changes)} times during load.")
        for i, entry in enumerate(cursor_changes[:5]):
            method_name, val, stack = entry
            print(f"\n--- change #{i + 1}: via {method_name} -> line {val} ---")
            print(stack)
        assert tree.cursor_line == before_load, (
            f"Cursor jumped from {before_load} to {tree.cursor_line} after "
            f"expand-during-load on real PDF."
        )
