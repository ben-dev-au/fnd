"""Prefetch-window vs cursor-position diagnostic.

Drives the app via Pilot. Builds a 20-file corpus, runs a query that
matches every file, then walks the result tree top-to-bottom one row
at a time with a generous settle between moves. After each move,
parses the diag log to record:

  * the prefetch_top window picked when the cursor anchored there;
  * whether the click landed on a cache hit (`cached=yes`) or a miss;
  * whether the worker actually finished decoding any new targets
    before the next move.

Emits a per-click table plus headline counts so we can see exactly
where the buffer stops following the cursor. NO code edits to
fnd/*; this is read-only diagnostic.

Run:
    ./.venv/bin/python tests/perf/bench_prefetch_window.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_here = Path(__file__).resolve()
sys.path.insert(0, str(_here.parent.parent.parent))

DIAG_PATH = Path("/tmp/fnd-preview-diag.log")
if DIAG_PATH.exists():
    DIAG_PATH.unlink()
os.environ["_FND_PREVIEW_DIAG"] = "1"
os.environ["_FND_REVEAL_FIRST"] = "1"

from fnd.config import Config, Defaults, RankingProfileConfig  # noqa: E402
from fnd.index import build_index  # noqa: E402
from fnd.tui import FNDApp  # noqa: E402
from tests.perf import _corpus  # noqa: E402

MATCH_TOKEN = _corpus.MATCH_TOKEN
N_FILES = int(os.environ.get("BENCH_N_FILES", "30"))
SETTLE_S = float(os.environ.get("BENCH_SETTLE_S", "1.2"))
CORPUS_KIND = os.environ.get("BENCH_CORPUS", "heavy_md")
# Kinds:
#   heavy_md  — 30 HEAVY md files (worst case for structural pre-mount)
#   mixed     — half md (SMALL+HEAVY), half txt
#   flat_txt  — all txt (flat path only)


def build_corpus(root: Path) -> Path:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    if CORPUS_KIND == "heavy_md":
        for i in range(N_FILES):
            # Vary `match_at_block` so chunks/section ordering differs
            # per-file; keeps Tantivy result fingerprints distinct.
            spec = _corpus.CorpusSpec(
                profile="heavy",
                headings=_corpus.HEAVY.headings,
                paragraphs_per_heading=_corpus.HEAVY.paragraphs_per_heading,
                table_count=_corpus.HEAVY.table_count,
                table_rows=_corpus.HEAVY.table_rows,
                table_cols=_corpus.HEAVY.table_cols,
                fence_count=_corpus.HEAVY.fence_count,
                fence_lines=_corpus.HEAVY.fence_lines,
                match_at_block=_corpus.HEAVY.match_at_block + i,
            )
            path = corpus / f"md_{i:02d}.md"
            path.write_text(_corpus.render(spec), encoding="utf-8")
    elif CORPUS_KIND == "flat_txt":
        for i in range(N_FILES):
            path = corpus / f"txt_{i:02d}.txt"
            target_line = 200 + i * 20
            lines = []
            for ln in range(500 + i * 50):
                if ln == target_line:
                    lines.append(f"Line {ln}: contains {MATCH_TOKEN} here.")
                else:
                    lines.append(
                        f"Line {ln}: filler content varied across files file_{i:02d} "
                        f"to keep document fingerprints distinct."
                    )
            path.write_text("\n".join(lines), encoding="utf-8")
    else:  # mixed
        specs = [_corpus.SMALL, _corpus.HEAVY]
        half = N_FILES // 2
        for i in range(half):
            spec = specs[i % len(specs)]
            path = corpus / f"md_{i:02d}_{spec.profile}.md"
            path.write_text(_corpus.render(spec), encoding="utf-8")
        for i in range(N_FILES - half):
            path = corpus / f"txt_{i:02d}.txt"
            target_line = 200 + i * 20
            lines = []
            for ln in range(500 + i * 50):
                if ln == target_line:
                    lines.append(f"Line {ln}: contains {MATCH_TOKEN} here.")
                else:
                    lines.append(
                        f"Line {ln}: filler content varied across files file_{i:02d} "
                        f"to keep document fingerprints distinct."
                    )
            path.write_text("\n".join(lines), encoding="utf-8")
    return corpus


@dataclass
class ClickRow:
    """One row of the diagnostic — what happened between move N and N+1."""

    click_idx: int
    anchor_parent: str = ""
    # Most recent prefetch_top before the cache_check for this click.
    prefetch_anchor: str | None = None
    prefetch_start_idx: int | None = None
    prefetch_targets: list[str] = field(default_factory=list)
    prefetch_already_cached: list[str] = field(default_factory=list)
    # Decode completions between this click and the next (worker progress).
    decode_done: list[str] = field(default_factory=list)
    # The cache_check verdict at click-time.
    cached: str = "?"
    cached_focus_in_widgets: bool | None = None
    path: str = "?"  # structural | flat | ?
    # Pre-mount queue activity in the interval BEFORE this click's
    # cache_check (i.e. while the user was sitting on the previous
    # row). Lets us see whether the drainer made progress between moves.
    premount_started: list[str] = field(default_factory=list)
    premount_skipped_cached: list[str] = field(default_factory=list)
    premount_queued: list[str] = field(default_factory=list)


def parse_diag(text: str, n_clicks: int) -> list[ClickRow]:
    """Walk the diag log line-by-line. The driver emits a unique marker
    `BENCH_MARK click=K` before each click so we can group reliably."""
    rows: list[ClickRow] = [ClickRow(click_idx=i) for i in range(n_clicks)]
    # Per-click "most recent" trackers — last prefetch_top before the
    # cache_check is the one that decided this click's window.
    cur_click: int | None = None

    def kv(line: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for tok in line.split():
            if "=" in tok:
                k, _, v = tok.partition("=")
                out[k] = v
        return out

    def parse_targets(s: str) -> list[str]:
        # "[a, b, c]" or "[]" — values are 8-char hex slices already.
        s = s.strip("[]")
        if not s:
            return []
        return [t.strip().strip("'\"") for t in s.split(",") if t.strip()]

    def parse_targets_in_line(line: str, key: str) -> list[str]:
        """Pull `key=[...]` from a line — split() breaks on commas inside the list."""
        m = re.search(rf"\b{key}=\[(.*?)\]", line)
        if not m:
            return []
        return parse_targets("[" + m.group(1) + "]")

    for line in text.splitlines():
        if "BENCH_MARK" in line:
            m = re.search(r"click=(\d+)\s+anchor=([a-f0-9]+)?", line)
            if m:
                cur_click = int(m.group(1))
                if cur_click < n_clicks:
                    rows[cur_click].anchor_parent = (m.group(2) or "")[:8]
            continue

        if cur_click is None or cur_click >= n_clicks:
            continue
        row = rows[cur_click]

        if line.startswith("prefetch_top "):
            d = kv(line)
            row.prefetch_anchor = d.get("anchor")
            try:
                row.prefetch_start_idx = int(d.get("start_idx", "-1"))
            except ValueError:
                row.prefetch_start_idx = None
            row.prefetch_targets = parse_targets_in_line(line, "targets")
            row.prefetch_already_cached = parse_targets_in_line(line, "already_cached")

        elif "prefetch_one done" in line:
            d = kv(line)
            parent = d.get("parent", "")
            if parent:
                row.decode_done.append(parent)

        elif "dispatch_preview cache_check" in line:
            d = kv(line)
            row.cached = d.get("cached", "?")
            fiw = d.get("focus_in_widgets")
            row.cached_focus_in_widgets = None if fiw is None else fiw == "True"
            row.path = "structural"

        elif line.startswith("dispatch_flat parent="):
            d = kv(line)
            row.cached = d.get("cached", "?")
            row.path = "flat"

        elif "prefetch_mount_structural_async STARTING" in line:
            d = kv(line)
            p = d.get("parent", "")
            if p:
                row.premount_started.append(p)
        elif "prefetch_mount_structural_async SKIPPED already-cached" in line:
            d = kv(line)
            p = d.get("parent", "")
            if p:
                row.premount_skipped_cached.append(p)
        elif "prefetch_mount_structural QUEUED" in line:
            d = kv(line)
            p = d.get("parent", "")
            if p:
                row.premount_queued.append(p)

    return rows


async def drive_app(corpus_root: Path) -> str:
    """Spawn the app, walk every result row top-to-bottom with a long
    settle, write BENCH_MARK lines into the diag log to bracket clicks."""
    from textual.widgets import Tree

    cfg = Config(
        defaults=Defaults(
            # Use a real (non-zero) debounce so behaviour matches what
            # the user experiences. Default is 150ms.
            preview_prefetch_count=10,
            preview_load_debounce_ms=150,
        ),
        ranking={"default": RankingProfileConfig()},
    )

    index_dir = corpus_root.parent / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[corpus_root], index_dir=index_dir, collection="default")

    app = FNDApp(
        index_dir=index_dir,
        config=cfg,
        collection="default",
        initial_query=MATCH_TOKEN,
    )

    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.5)

        tree = app.query_one("#results_pane", Tree)
        for _ in range(60):
            await pilot.pause()
            await asyncio.sleep(0.05)
            if len(tree.root.children) >= N_FILES:
                break

        results = list(tree.root.children)
        if len(results) < max(12, N_FILES - 4):
            return f"ERROR: need >={max(12, N_FILES - 4)} results, got {len(results)}\n"
        # Click all results we got; bench is meaningful for any N>=12.

        # Let initial prefetch settle before clicking.
        await asyncio.sleep(0.8)

        groups = app._groups  # type: ignore[attr-defined]
        for i, node in enumerate(results):
            parent_id = groups[i].parent_id if i < len(groups) else ""
            # Snapshot cache sizes BEFORE the click so we can see what
            # state the prefetch left us in for this row.
            pc = len(app._preview_cache._cache)  # type: ignore[attr-defined]
            cc = len(app._chunk_cache)  # type: ignore[attr-defined]
            fb = len(app._flat_buffer_cache)  # type: ignore[attr-defined]
            app._diag_log(  # type: ignore[attr-defined]
                f"BENCH_MARK click={i} anchor={parent_id} "
                f"preview_cache_n={pc} chunk_cache_n={cc} flat_buf_n={fb}"
            )
            tree.cursor_line = node.line
            # Settle: pilot.pause loop + a full SETTLE_S sleep so the
            # debounce timer + prefetch worker have a chance to finish.
            for _ in range(40):
                await pilot.pause()
                await asyncio.sleep(0.05)
            await asyncio.sleep(SETTLE_S)

        # Final flush.
        await asyncio.sleep(0.5)
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.05)

    return DIAG_PATH.read_text() if DIAG_PATH.exists() else ""


def render_table(rows: list[ClickRow]) -> str:
    out: list[str] = []
    out.append("\n" + "─" * 100)
    out.append(f"Per-click prefetch window vs cursor ({len(rows)} clicks)")
    out.append("─" * 100)
    out.append(
        f"{'i':>2} {'anchor':>8} {'cached':>7} {'fiw':>5} {'path':>10} "
        f"{'pf_n':>4} {'in_win':>6} {'pm_q':>4} {'pm_s':>4} {'pm_sk':>5}"
    )
    out.append("─" * 100)
    for r in rows:
        in_win = (
            "yes"
            if r.anchor_parent and r.anchor_parent in r.prefetch_targets
            else (
                "cached"
                if r.anchor_parent and r.anchor_parent in r.prefetch_already_cached
                else "NO"
            )
        )
        out.append(
            f"{r.click_idx:>2} {r.anchor_parent:>8} {r.cached:>7} "
            f"{r.cached_focus_in_widgets!s:>5} {r.path:>10} "
            f"{len(r.prefetch_targets):>4} {in_win:>6} "
            f"{len(r.premount_queued):>4} {len(r.premount_started):>4} "
            f"{len(r.premount_skipped_cached):>5}"
        )
    return "\n".join(out)


def render_headlines(rows: list[ClickRow]) -> str:
    misses = [r for r in rows if r.cached == "no"]
    structural_miss = [r for r in misses if r.path == "structural"]
    flat_miss = [r for r in misses if r.path == "flat"]
    out: list[str] = []
    out.append("\n" + "─" * 100)
    out.append("Headline")
    out.append("─" * 100)
    out.append(f"  Total clicks       : {len(rows)}")
    out.append(f"  Cache MISS on click: {len(misses)}  indices={[r.click_idx for r in misses]}")
    out.append(f"    structural misses: {len(structural_miss)}")
    out.append(f"    flat misses      : {len(flat_miss)}")
    # Did prefetch ever even contain the cursor row?
    cursor_in_window = sum(
        1
        for r in rows
        if r.anchor_parent
        and (r.anchor_parent in r.prefetch_targets or r.anchor_parent in r.prefetch_already_cached)
    )
    out.append(f"  Cursor row in prefetch window post-move: {cursor_in_window}/{len(rows)}")
    # Decode throughput
    total_decoded = sum(len(r.decode_done) for r in rows)
    out.append(f"  Total decode completions across run: {total_decoded}")
    return "\n".join(out)


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fnd-prefetch-window-") as tmp:
        root = Path(tmp)
        corpus = build_corpus(root)
        diag = await drive_app(corpus)
    if not diag:
        print("ERROR: no diag captured", file=sys.stderr)
        return 1
    # Count BENCH_MARK lines to size the row table to the actual clicks.
    n_clicks = sum(1 for ln in diag.splitlines() if "BENCH_MARK click=" in ln)
    rows = parse_diag(diag, max(n_clicks, 1))
    print(render_table(rows))
    print(render_headlines(rows))
    # Also tail the raw log for context.
    print("\n" + "─" * 100)
    print("Raw diag tail (last 80 lines):")
    print("─" * 100)
    print("\n".join(diag.splitlines()[-80:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
