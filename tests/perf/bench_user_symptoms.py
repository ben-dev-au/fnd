"""User-symptom harness — measures the things the user reports.

For each cursor move, captures:

  T0   click moment (cursor_line set)
  T1   border_title contains the expected filename
  T2   focused chunk widget exists in the DOM (mounted)
  T3   focused chunk's first_match_block resolves (match locatable)
  T4   focused widget has region.height > 0 (laid out, on-screen)
  T5   do_scroll fires for this focus_seq (scroll-to-match completes)

Also captures any exceptions logged to the app's logger or stderr
during the run. Designed to surface user-visible symptoms that the
cache-hit/miss bench misses: title-not-updating, slow first paint,
match-not-visible.

Run:
    ./.venv/bin/python tests/perf/bench_user_symptoms.py
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
import tempfile
import time
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
from fnd.tui.app import FNDMarkdown, PreviewContainer  # noqa: E402
from tests.perf import _corpus  # noqa: E402

MATCH_TOKEN = _corpus.MATCH_TOKEN
N_FILES = int(os.environ.get("BENCH_N_FILES", "20"))
TIMEOUT_S = float(os.environ.get("BENCH_TIMEOUT_S", "8.0"))
INTER_CLICK_PAD_S = float(os.environ.get("BENCH_PAD_S", "0.3"))


def build_corpus(root: Path) -> Path:
    """Heavy md files — mimics a real-world corpus with deep
    structural content per file."""
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for i in range(N_FILES):
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
        (corpus / f"md_{i:02d}.md").write_text(_corpus.render(spec), encoding="utf-8")
    return corpus


@dataclass
class ClickReport:
    idx: int
    parent_id: str = ""
    expected_basename: str = ""
    focus_seq: int = 0
    # Wall-clock seconds from click moment to each milestone (None if never).
    t_title: float | None = None
    t_focused_widget: float | None = None
    t_first_match_block: float | None = None
    t_widget_visible: float | None = None
    t_do_scroll: float | None = None
    # State at click moment.
    in_preview_cache: bool = False
    is_complete_at_click: bool | None = None
    focus_in_widgets_at_click: bool | None = None
    # Errors during/after the click.
    errors: list[str] = field(default_factory=list)


async def _poll_milestones(
    app: FNDApp,
    report: ClickReport,
    deadline: float,
) -> None:
    """Watch the app until every milestone is hit or deadline expires."""
    expected = report.expected_basename
    focus_seq = report.focus_seq
    t0 = time.perf_counter() - 0  # caller set t0 just before

    # We'll inspect the diag log for do_scroll completion separately
    # (after the run); this poll covers DOM state only.
    while time.perf_counter() < deadline:
        now_rel = time.perf_counter() - t0
        # Title
        if report.t_title is None:
            try:
                pane = app.query_one("#preview_pane")
                title = str(getattr(pane, "border_title", "") or "")
                if expected and expected in title:
                    report.t_title = now_rel
            except Exception:
                pass
        # Focused widget exists & visible & first_match resolved
        if (
            report.t_focused_widget is None
            or report.t_first_match_block is None
            or report.t_widget_visible is None
        ):
            container = app._active_preview  # type: ignore[attr-defined]
            if isinstance(container, PreviewContainer):
                widget = container.chunk_widgets.get(focus_seq)
                if widget is not None:
                    if report.t_focused_widget is None:
                        report.t_focused_widget = now_rel
                    if isinstance(widget, FNDMarkdown):
                        if (
                            report.t_first_match_block is None
                            and widget.first_match_block is not None
                        ):
                            report.t_first_match_block = now_rel
                    else:
                        # Non-FNDMarkdown focused widget (e.g. table DT
                        # registered itself) — treat existence as match
                        # resolved.
                        if report.t_first_match_block is None:
                            report.t_first_match_block = now_rel
                    if report.t_widget_visible is None:
                        try:
                            if widget.region.height > 0:
                                report.t_widget_visible = now_rel
                        except Exception:
                            pass
        # All done?
        if (
            report.t_title is not None
            and report.t_focused_widget is not None
            and report.t_first_match_block is not None
            and report.t_widget_visible is not None
        ):
            return
        await asyncio.sleep(0.02)


def _capture_log_excerpts() -> tuple[logging.Handler, io.StringIO]:
    """Attach a string handler to the root + textual loggers so we
    catch exception traces during the run."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    for name in ("", "textual", "fnd"):
        logging.getLogger(name).addHandler(h)
    return h, buf


def _release_log_handler(h: logging.Handler) -> None:
    for name in ("", "textual", "fnd"):
        try:
            logging.getLogger(name).removeHandler(h)
        except Exception:
            pass


async def drive(corpus_root: Path) -> tuple[list[ClickReport], str, str]:
    """Pilot drive; returns (reports, diag_log_text, captured_warnings)."""
    from textual.widgets import Tree

    cfg = Config(
        defaults=Defaults(
            preview_prefetch_count=10,
            preview_load_debounce_ms=150,
        ),
        ranking={"default": RankingProfileConfig()},
    )

    index_dir = corpus_root.parent / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    build_index(roots=[corpus_root], index_dir=index_dir, collection="default")

    log_handler, log_buf = _capture_log_excerpts()

    app = FNDApp(
        index_dir=index_dir,
        config=cfg,
        collection="default",
        initial_query=MATCH_TOKEN,
    )

    reports: list[ClickReport] = []
    try:
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.5)

            tree = app.query_one("#results_pane", Tree)
            for _ in range(60):
                await pilot.pause()
                await asyncio.sleep(0.05)
                if len(tree.root.children) >= N_FILES - 4:
                    break
            results = list(tree.root.children)
            if not results:
                return [], "", "ERROR: no results"

            await asyncio.sleep(0.8)
            groups = app._groups  # type: ignore[attr-defined]

            for i, node in enumerate(results[: min(N_FILES, len(results))]):
                if i >= len(groups):
                    break
                g = groups[i]
                report = ClickReport(
                    idx=i,
                    parent_id=g.parent_id,
                    expected_basename=Path(g.path).name,
                    focus_seq=g.hits[0].chunk_seq if g.hits else 0,
                )
                # Snapshot cache state pre-click.
                report.in_preview_cache = (
                    app._preview_cache.get(  # type: ignore[attr-defined]
                        g.parent_id,
                        app._current_query_signature(),  # type: ignore[attr-defined]
                    )
                    is not None
                )

                app._diag_log(  # type: ignore[attr-defined]
                    f"BENCH_CLICK idx={i} parent={g.parent_id} "
                    f"focus_seq={report.focus_seq} pre_cached={report.in_preview_cache}"
                )

                t0 = time.perf_counter()
                tree.cursor_line = node.line
                deadline = t0 + TIMEOUT_S
                await _poll_milestones(app, report, deadline)
                # Final state snapshot.
                container = app._active_preview  # type: ignore[attr-defined]
                if isinstance(container, PreviewContainer):
                    report.is_complete_at_click = container.is_complete
                    report.focus_in_widgets_at_click = report.focus_seq in container.chunk_widgets
                # Inter-click pad.
                await asyncio.sleep(INTER_CLICK_PAD_S)
                reports.append(report)

            # Final settle for the last click's effects.
            await asyncio.sleep(0.5)
            for _ in range(20):
                await pilot.pause()
                await asyncio.sleep(0.05)
    finally:
        _release_log_handler(log_handler)

    diag = DIAG_PATH.read_text() if DIAG_PATH.exists() else ""
    # Cross-reference diag log for do_scroll completion per click.
    _attach_do_scroll_times(reports, diag)
    _attach_errors_from_diag(reports, diag)
    return reports, diag, log_buf.getvalue()


def _attach_do_scroll_times(reports: list[ClickReport], diag: str) -> None:
    """Walk the diag log; for each BENCH_CLICK, find the next matching
    do_scroll with the same focus_seq and record the relative time."""
    lines = diag.splitlines()
    # Parse timestamps and find click+do_scroll pairs.
    cur_idx: int | None = None
    cur_t0: float | None = None
    cur_focus: int | None = None
    for line in lines:
        m_ts = re.match(r"^\[(\d+\.\d+)\] (.*)$", line)
        if not m_ts:
            continue
        ts = float(m_ts.group(1))
        body = m_ts.group(2)
        if body.startswith("BENCH_CLICK"):
            m = re.search(r"idx=(\d+).*focus_seq=(\d+)", body)
            if m:
                cur_idx = int(m.group(1))
                cur_t0 = ts
                cur_focus = int(m.group(2))
        elif cur_idx is not None and cur_t0 is not None and cur_focus is not None:
            m_ds = re.search(r"do_scroll seq=(\d+).*retries_used=(\d+)", body)
            if m_ds and int(m_ds.group(1)) == cur_focus and cur_idx < len(reports):
                if reports[cur_idx].t_do_scroll is None:
                    reports[cur_idx].t_do_scroll = ts - cur_t0


def _attach_errors_from_diag(reports: list[ClickReport], diag: str) -> None:
    """Tag a click with any FAILED/exception lines in its window."""
    lines = diag.splitlines()
    cur_idx: int | None = None
    for line in lines:
        m_ts = re.match(r"^\[\d+\.\d+\] (.*)$", line)
        body = m_ts.group(1) if m_ts else line
        if body.startswith("BENCH_CLICK"):
            m = re.search(r"idx=(\d+)", body)
            if m:
                cur_idx = int(m.group(1))
        elif cur_idx is not None and cur_idx < len(reports):
            if re.search(r"FAIL|exception|Traceback|threw:|deadlock", body, re.I):
                reports[cur_idx].errors.append(body)


def render(reports: list[ClickReport]) -> str:
    out: list[str] = []
    out.append("\n" + "─" * 120)
    out.append(f"User-symptom harness ({len(reports)} clicks)")
    out.append("─" * 120)
    out.append(
        f"{'i':>2} {'pre':>3} {'cmpl':>4} {'fiw':>4} "
        f"{'t_title':>8} {'t_widget':>9} {'t_match':>8} {'t_visib':>8} {'t_scroll':>9}"
        f"  filename"
    )
    out.append("─" * 120)

    def fmt(t: float | None) -> str:
        if t is None:
            return "  —  "
        return f"{t:>6.2f}s"

    for r in reports:
        out.append(
            f"{r.idx:>2} {('Y' if r.in_preview_cache else 'n'):>3} "
            f"{(str(r.is_complete_at_click)[0] if r.is_complete_at_click is not None else '-'):>4} "
            f"{(str(r.focus_in_widgets_at_click)[0] if r.focus_in_widgets_at_click is not None else '-'):>4} "
            f"{fmt(r.t_title):>8} {fmt(r.t_focused_widget):>9} "
            f"{fmt(r.t_first_match_block):>8} {fmt(r.t_widget_visible):>8} "
            f"{fmt(r.t_do_scroll):>9}  {r.expected_basename}"
        )
        for err in r.errors[:3]:
            out.append(f"     ! {err[:110]}")

    out.append("─" * 120)
    # Headline stats.
    have_t = [r for r in reports if r.t_widget_visible is not None]
    if have_t:
        ts = sorted(r.t_widget_visible for r in have_t if r.t_widget_visible is not None)
        out.append(
            f"t_widget_visible: p50={ts[len(ts) // 2]:.2f}s "
            f"p95={ts[int(len(ts) * 0.95)] if len(ts) > 1 else ts[-1]:.2f}s "
            f"max={ts[-1]:.2f}s  ({len(have_t)}/{len(reports)} reached)"
        )
    title_missed = [r for r in reports if r.t_title is None]
    if title_missed:
        out.append(
            f"Title NOT updated within timeout: {len(title_missed)}/{len(reports)} clicks: {[r.idx for r in title_missed]}"
        )
    match_missed = [r for r in reports if r.t_first_match_block is None]
    if match_missed:
        out.append(
            f"first_match_block NOT resolved within timeout: {len(match_missed)}/{len(reports)} clicks: {[r.idx for r in match_missed]}"
        )
    any_errors = [r for r in reports if r.errors]
    if any_errors:
        out.append(f"Errors in {len(any_errors)} clicks: {[r.idx for r in any_errors]}")
    return "\n".join(out)


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fnd-symptoms-") as tmp:
        root = Path(tmp)
        corpus = build_corpus(root)
        reports, _diag, log_text = await drive(corpus)
    print(render(reports))
    if log_text.strip():
        print("\n--- Captured warnings/errors during run ---")
        print(log_text[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
