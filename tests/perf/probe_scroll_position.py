"""Probe — capture wrong-position scrolls on intra-file match navigation.

For each section-node click in an active file, records:
  - the chunk_seq the click was for
  - the resolved scroll target widget (path through first_match_block /
    fallback descendant-scan / _scroll_proxy_for)
  - the pane's scroll_offset.y before and after the click
  - the y of the resolved target relative to the pane viewport
  - the y of the chunk's HEADER widget (where chunk_to_range starts)
  - the y of the chunk's first_match_block (if set)
  - retries_used inside _do_scroll_to_chunk

Used to identify HOW the scroll position can be wrong: stale
first_match_block, race against retry budget, target.region.height==0,
fallback descendant-scan landing on the wrong widget, etc.

Run with:
    ./.venv/bin/python tests/perf/probe_scroll_position.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402

VAULT_ROOT = Path(
    "/Users/BenDavidson/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
)


def build_vault_subset(root: Path, *, n: int, query_substr: str | None = None) -> Path:
    if not VAULT_ROOT.exists():
        raise SystemExit(f"vault root not found: {VAULT_ROOT}")
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    md_files: list[tuple[int, Path]] = []
    for p in VAULT_ROOT.rglob("*.md"):
        try:
            md_files.append((p.stat().st_size, p))
        except OSError:
            continue
    md_files.sort(key=lambda t: t[0], reverse=True)
    # Copy the largest 6 — likely to have many chunks each with multiple matches.
    for _size, src in md_files[:n]:
        shutil.copy2(src, corpus / src.name.replace("/", "_"))
    return corpus


def install_scroll_probe(app: Any, log: list[dict[str, Any]]) -> None:
    """Wrap pane.scroll_to_widget to record what target was passed."""
    from textual.containers import VerticalScroll

    pane = app.query_one("#preview_pane", VerticalScroll)
    orig_scroll = pane.scroll_to_widget

    def _wrapped(widget: Any, **kwargs: Any) -> Any:
        try:
            scroll_before = int(pane.scroll_offset.y)
        except Exception:
            scroll_before = -1
        result = orig_scroll(widget, **kwargs)
        try:
            scroll_after = int(pane.scroll_offset.y)
        except Exception:
            scroll_after = -1
        try:
            widget_y = int(widget.region.y) if widget.region else -1
            widget_h = int(widget.region.height) if widget.region else 0
        except Exception:
            widget_y = -1
            widget_h = 0
        log.append(
            {
                "event": "scroll_to_widget",
                "widget_type": type(widget).__name__,
                "widget_y": widget_y,
                "widget_h": widget_h,
                "scroll_before": scroll_before,
                "scroll_after": scroll_after,
                "kwargs": kwargs,
            }
        )
        return result

    pane.scroll_to_widget = _wrapped  # type: ignore[method-assign]
    return orig_scroll


async def probe(label: str, query: str, n_files: int, intra_clicks: int) -> int:
    with tempfile.TemporaryDirectory(prefix="fnd-scroll-") as tmp:
        root = Path(tmp)
        corpus = build_vault_subset(root, n=n_files)
        index_dir = root / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(roots=[corpus], index_dir=index_dir, collection="default")
        cfg = Config(
            defaults=Defaults(preview_prefetch_count=0, preview_load_debounce_ms=0),
            ranking={"default": RankingProfileConfig()},
        )
        app = FNDApp(index_dir=index_dir, config=cfg, collection="default", initial_query=query)
        from textual.widgets import Tree  # pyright: ignore[reportMissingImports]
        from textual.widgets.tree import TreeNode  # pyright: ignore[reportMissingImports]

        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            tree = app.query_one("#results_pane", Tree)
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if len(tree.root.children) >= 1:
                    break
            results = list(tree.root.children)
            if not results:
                print(f"{label}: no results for query={query!r}")
                return 1

            # Click the first FILE to make it active. Wait for mount.
            tree.cursor_line = results[0].line
            await asyncio.sleep(3.0)

            # Install the scroll probe AFTER initial mount so we only
            # capture intra-file navigation.
            log: list[dict[str, Any]] = []
            install_scroll_probe(app, log)

            # Expand the file row and click each section.
            file_node = results[0]
            file_node.expand()
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if file_node.children:
                    break
            sections = list(file_node.children)[:intra_clicks]
            if not sections:
                print(f"{label}: no sections under {file_node.label}")
                return 1

            _ = TreeNode  # ensure the import is exercised; type kept narrow elsewhere
            for i, sec in enumerate(sections):
                sec_data = sec.data
                chunk_seq = -1
                if isinstance(sec_data, dict) and sec_data.get("kind") == "section":
                    hit = sec_data["hit"]
                    chunk_seq = hit.chunk_seq
                # Snapshot pre-click state.
                pre_log_idx = len(log)
                tree.cursor_line = sec.line
                # Wait for the scroll to fire (retry budget can extend this).
                await asyncio.sleep(0.8)
                # Capture pertinent state about the chunk.
                active = app._active_preview
                chunk_md = app._chunk_widgets.get(chunk_seq) if active is not None else None
                from fnd.tui.app import FNDMarkdown

                fmb_type = None
                fmb_y = None
                fmb_h = None
                if isinstance(chunk_md, FNDMarkdown):
                    inner = chunk_md.first_match_block
                    if inner is not None:
                        fmb_type = type(inner).__name__
                        try:
                            fmb_y = int(inner.region.y)
                            fmb_h = int(inner.region.height)
                        except Exception:
                            fmb_y, fmb_h = None, None
                chunk_y = None
                chunk_h = None
                if chunk_md is not None:
                    try:
                        chunk_y = int(chunk_md.region.y)
                        chunk_h = int(chunk_md.region.height)
                    except Exception:
                        chunk_y, chunk_h = None, None
                # Calls to scroll_to_widget for this click.
                scrolls = log[pre_log_idx:]
                pane = app.query_one("#preview_pane")
                final_scroll = int(pane.scroll_offset.y)
                print(
                    f"\n[{label}] click #{i} chunk_seq={chunk_seq} "
                    f"chunk_widget={'yes' if chunk_md is not None else 'no'} "
                    f"final_scroll_y={final_scroll}"
                )
                print(
                    f"  chunk_md.region y={chunk_y} h={chunk_h}   "
                    f"first_match_block={fmb_type} y={fmb_y} h={fmb_h}"
                )
                if not scrolls:
                    print("  scroll_to_widget calls: 0  (NO SCROLL FIRED)")
                for s in scrolls:
                    print(
                        f"  scroll_to_widget -> {s['widget_type']:24s} "
                        f"widget_y={s['widget_y']:4d} h={s['widget_h']:3d}  "
                        f"scroll {s['scroll_before']:4d} -> {s['scroll_after']:4d}"
                    )

        return 0


async def main() -> int:
    # Use a common-word query so heavy files yield many sections-per-file.
    return await probe(label="vault", query="the", n_files=4, intra_clicks=10)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
