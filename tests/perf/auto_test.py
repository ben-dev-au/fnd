"""Automated preview-perf test harness.

Drives the app via Pilot against a mixed synthetic corpus (md + txt
to exercise both structural and flat paths). Captures diag log,
parses key metrics, and reports findings. Designed to let
investigation iterate without manual user input.

Run with:

    ./.venv/bin/python tests/perf/auto_test.py

Outputs metrics to stdout and a full diag log to /tmp/auto-test-diag.log.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

# _diag_log writes to a hard-coded path in app.py.
DIAG_PATH = Path("/tmp/acorn-preview-diag.log")
if DIAG_PATH.exists():
    DIAG_PATH.unlink()
os.environ["ACORN_PREVIEW_DIAG"] = "1"
os.environ["ACORN_REVEAL_FIRST"] = "1"

from acorn.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from acorn.index import build_index  # noqa: E402
from acorn.tui import AcornApp  # noqa: E402

from tests.perf import _corpus  # noqa: E402

MATCH_TOKEN = _corpus.MATCH_TOKEN


def build_mixed_corpus(root: Path, *, n_md: int = 6, n_txt: int = 6) -> Path:
    """Write n_md markdown files and n_txt plain text files. Every file
    embeds MATCH_TOKEN once at a known position."""
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    # Markdown files — varied profiles to exercise different structural paths.
    md_specs = [_corpus.SMALL, _corpus.HEAVY, _corpus.TABLE_HEAVY, _corpus.FENCE_HEAVY]
    for i in range(n_md):
        spec = md_specs[i % len(md_specs)]
        path = corpus / f"md_{i:02d}_{spec.profile}.md"
        path.write_text(_corpus.render(spec), encoding="utf-8")
    # Plain text files — exercise the flat-buffer path the same way PDFs do.
    for i in range(n_txt):
        path = corpus / f"txt_{i:02d}.txt"
        lines = []
        target_line = 200 + i * 20
        for ln in range(500 + i * 100):
            if ln == target_line:
                lines.append(f"Line {ln}: contains {MATCH_TOKEN} here.")
            else:
                lines.append(
                    f"Line {ln}: filler content with some words to make this look "
                    f"like a real document with paragraphs of reasonable length."
                )
        path.write_text("\n".join(lines), encoding="utf-8")
    return corpus


@dataclass
class ClickMetrics:
    parent: str
    path: str  # "structural"/"flat"
    cached: str  # "yes"/"no"
    focus_in_widgets: bool | None
    is_complete: bool | None
    finalize_elapsed_ms: float | None
    finalize_wait_ms: float | None
    do_scroll_count: int
    do_scroll_retries: list[int]
    do_scroll_max_retries: int
    pre_reveal_lifted: bool  # legacy field — pre-reveal-lift code removed
    miss_zero_region: bool
    post_layout_size: str | None
    post_layout_virtual_size: str | None


def parse_diag(text: str) -> list[ClickMetrics]:
    """Walk the diag log and group events by click. Each click starts
    with a ``dispatch_preview cache_check`` or ``dispatch_flat`` line."""
    lines = text.splitlines()
    metrics: list[ClickMetrics] = []
    cur: ClickMetrics | None = None

    def parse_kv(line: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for tok in line.split():
            if "=" in tok:
                k, _, v = tok.partition("=")
                out[k] = v
        return out

    def close() -> None:
        nonlocal cur
        if cur is not None:
            metrics.append(cur)
            cur = None

    for line in lines:
        if "dispatch_preview cache_check" in line:
            close()
            kv = parse_kv(line)
            cur = ClickMetrics(
                parent=kv.get("parent", "")[:8],
                path="structural",
                cached=kv.get("cached", "?"),
                focus_in_widgets=kv.get("focus_in_widgets") == "True",
                is_complete=(
                    None if kv.get("is_complete") in {"None", None} else kv.get("is_complete") == "True"
                ),
                finalize_elapsed_ms=None,
                finalize_wait_ms=None,
                do_scroll_count=0,
                do_scroll_retries=[],
                do_scroll_max_retries=0,
                pre_reveal_lifted=False,
                miss_zero_region=False,
                post_layout_size=None,
                post_layout_virtual_size=None,
            )
        elif "dispatch_flat " in line and "cache_hit" not in line and "prebuilt" not in line and "cold" not in line and "post_layout" not in line:
            # The header line of a flat dispatch
            close()
            kv = parse_kv(line)
            cur = ClickMetrics(
                parent=kv.get("parent", "")[:8],
                path="flat",
                cached=kv.get("cached", "?"),
                focus_in_widgets=None,
                is_complete=None,
                finalize_elapsed_ms=None,
                finalize_wait_ms=None,
                do_scroll_count=0,
                do_scroll_retries=[],
                do_scroll_max_retries=0,
                pre_reveal_lifted=False,
                miss_zero_region=False,
                post_layout_size=None,
                post_layout_virtual_size=None,
            )
        elif cur is not None:
            if "finalize_pre_reveal done" in line:
                m = re.search(r"elapsed_ms=([\d.]+)", line)
                w = re.search(r"wait_ms=([\d.]+)", line)
                if m:
                    cur.finalize_elapsed_ms = float(m.group(1))
                if w:
                    cur.finalize_wait_ms = float(w.group(1))
            elif "do_scroll" in line and "retries_used" in line:
                m = re.search(r"retries_used=(\d+)", line)
                if m:
                    used = int(m.group(1))
                    cur.do_scroll_count += 1
                    cur.do_scroll_retries.append(used)
                    cur.do_scroll_max_retries = max(cur.do_scroll_max_retries, used)
            elif "miss=zero-region" in line:
                cur.miss_zero_region = True
            elif "pre_reveal_lifted" in line:
                cur.pre_reveal_lifted = True
            elif "dispatch_flat post_layout" in line:
                m_size = re.search(r"\bsize=(Size\([^)]*\))", line)
                m_vs = re.search(r"virtual_size=(Size\([^)]*\))", line)
                if m_size:
                    cur.post_layout_size = m_size.group(1)
                if m_vs:
                    cur.post_layout_virtual_size = m_vs.group(1)
    close()
    return metrics


async def drive_app(corpus_root: Path, *, n_clicks: int = 10) -> str:
    """Spawn the app via Pilot, run the query, click N results, return
    the diag log contents."""
    from textual.widgets import Tree

    cfg = Config(
        defaults=Defaults(
            preview_prefetch_count=10,
            preview_load_debounce_ms=0,
        ),
        ranking={"default": RankingProfileConfig()},
    )

    index_dir = corpus_root.parent / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[corpus_root], index_dir=index_dir, collection="default")

    app = AcornApp(
        index_dir=index_dir,
        config=cfg,
        collection="default",
        initial_query=MATCH_TOKEN,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.5)

        # Wait for results.
        tree = app.query_one("#results_pane", Tree)
        for _ in range(30):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if len(tree.root.children) > 0:
                break

        results = list(tree.root.children)
        if not results:
            return "ERROR: no results after query\n"

        # Give prefetch time to start populating cache before first click.
        await asyncio.sleep(0.5)

        # Click each result in sequence. Pause between clicks so each
        # one fully resolves before the next click overlaps it — the
        # goal is to measure single-click behaviour cleanly, not
        # debouncing.
        for i, node in enumerate(results[:n_clicks]):
            tree.cursor_line = node.line
            await pilot.pause()
            tree.post_message(Tree.NodeSelected(node))
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)

        # Final settle.
        await asyncio.sleep(0.5)
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.05)

    return DIAG_PATH.read_text() if DIAG_PATH.exists() else ""


def summarize(metrics: list[ClickMetrics]) -> str:
    """Human-readable summary. Highlights the metrics that matter most:
    cold-path elapsed_ms, cached scroll count, flat-path post-layout
    size."""
    out: list[str] = []
    out.append(f"\n{'─' * 80}")
    out.append(f"Click summary ({len(metrics)} clicks)")
    out.append(f"{'─' * 80}")
    cold = [m for m in metrics if m.finalize_elapsed_ms is not None]
    cached_struct = [m for m in metrics if m.path == "structural" and m.cached == "yes" and m.finalize_elapsed_ms is None]
    flat_clicks = [m for m in metrics if m.path == "flat"]

    out.append(f"\nCold-path clicks ({len(cold)}):")
    for m in cold:
        wait = f"wait={m.finalize_wait_ms:.0f}ms" if m.finalize_wait_ms is not None else "wait=?"
        out.append(
            f"  {m.parent} {wait} elapsed={m.finalize_elapsed_ms:.0f}ms "
            f"scrolls={m.do_scroll_count} retries={m.do_scroll_retries} "
            f"zero_region={m.miss_zero_region}"
        )

    out.append(f"\nCached structural clicks ({len(cached_struct)}):")
    for m in cached_struct:
        out.append(
            f"  {m.parent} focus_in_widgets={m.focus_in_widgets} "
            f"scrolls={m.do_scroll_count} retries={m.do_scroll_retries}"
        )

    out.append(f"\nFlat-path clicks ({len(flat_clicks)}):")
    for m in flat_clicks:
        size = m.post_layout_size or "?"
        vs = m.post_layout_virtual_size or "?"
        out.append(f"  {m.parent} cached={m.cached} post_layout_size={size} virtual={vs}")

    # Headline stats.
    out.append(f"\n{'─' * 80}")
    out.append("Headline stats:")
    if cold:
        max_cold = max(m.finalize_elapsed_ms for m in cold)
        avg_cold = sum(m.finalize_elapsed_ms for m in cold) / len(cold)
        out.append(f"  Cold-path max={max_cold:.0f}ms avg={avg_cold:.0f}ms")
        zero_region_cold = sum(1 for m in cold if m.miss_zero_region)
        out.append(f"  Cold clicks hitting zero-region: {zero_region_cold}/{len(cold)}")
    if cached_struct:
        avg_scrolls = sum(m.do_scroll_count for m in cached_struct) / len(cached_struct)
        out.append(f"  Cached structural avg scrolls/click: {avg_scrolls:.1f}")
    if flat_clicks:
        broken = [m for m in flat_clicks if m.post_layout_size and "height=0" in m.post_layout_size]
        out.append(f"  Flat clicks with post-layout height=0: {len(broken)}/{len(flat_clicks)}")

    return "\n".join(out)


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="acorn-auto-test-") as tmp:
        root = Path(tmp)
        corpus = build_mixed_corpus(root)
        diag = await drive_app(corpus, n_clicks=10)
    if not diag:
        print("ERROR: no diag captured", file=sys.stderr)
        return 1
    print(diag[-4000:])  # tail of full diag
    metrics = parse_diag(diag)
    print(summarize(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
