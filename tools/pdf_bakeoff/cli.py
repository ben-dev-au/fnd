"""Bake-off CLI. Run PDF extractors over a corpus; emit CSV + side-by-side MD."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
import resource
import statistics
import sys
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pymupdf  # type: ignore[import-untyped]

from tools.pdf_bakeoff import runners as runner_registry
from tools.pdf_bakeoff.metrics import (
    CSV_COLUMNS,
    RunnerResult,
    populate_structural_metrics,
)

try:
    import mdformat  # type: ignore[import-not-found]
except ImportError:
    mdformat = None  # type: ignore[assignment]


def _normalize_md(md: str) -> str:
    """Run extractor output through mdformat for consistent spacing/lists.

    Doesn't recover lost structure; only normalises what's there. Skipped
    silently if mdformat isn't installed.
    """
    if not md or mdformat is None:
        return md
    try:
        return mdformat.text(md)
    except Exception:
        # Some extractors emit Markdown that mdformat's strict parser
        # rejects. Fall back to the original on failure.
        return md


DEFAULT_RUNNERS = "baseline,pymupdf4llm_layout,pymupdf4llm_legacy,pymupdf4llm_toc"


@dataclass
class Args:
    pdf_dir: Path
    out_dir: Path
    runners: list[str]
    pages_per_pdf: int  # 0 = all
    seed: int
    max_pdfs: int | None
    include_glob: str


def parse_args(argv: Sequence[str] | None = None) -> Args:
    p = argparse.ArgumentParser(
        prog="python -m tools.pdf_bakeoff",
        description=(
            "Measure PDF extractor candidates on a real corpus. "
            "Phase 0 of the real-PDF-support workstream."
        ),
    )
    p.add_argument("pdf_dir", type=Path, help="directory of PDFs (recursive)")
    p.add_argument("out_dir", type=Path, help="output directory for CSV / MD")
    p.add_argument(
        "--runners",
        default=DEFAULT_RUNNERS,
        help=f"comma-separated runner names. default: {DEFAULT_RUNNERS}",
    )
    p.add_argument("--with-docling", action="store_true", help="enable docling runner")
    p.add_argument("--with-marker", action="store_true", help="enable marker runner")
    p.add_argument("--with-mineru", action="store_true", help="enable mineru runner")
    p.add_argument(
        "--pages-per-pdf",
        type=int,
        default=5,
        help="sampled pages per PDF for side-by-side output; 0 = all pages",
    )
    p.add_argument("--seed", type=int, default=42, help="rng seed for page sampling")
    p.add_argument("--max-pdfs", type=int, default=None, help="cap number of PDFs processed")
    p.add_argument(
        "--include-glob",
        default="**/*.pdf",
        help="glob to filter PDF files under pdf_dir",
    )
    ns = p.parse_args(argv)

    requested = [n.strip() for n in ns.runners.split(",") if n.strip()]
    opt_ins: set[str] = set()
    if ns.with_docling:
        opt_ins.add("docling")
        if "docling" not in requested:
            requested.append("docling")
    if ns.with_marker:
        opt_ins.add("marker")
        if "marker" not in requested:
            requested.append("marker")
    if ns.with_mineru:
        opt_ins.add("mineru")
        if "mineru" not in requested:
            requested.append("mineru")

    # baseline always runs; it's the jaccard denominator.
    if "baseline" not in requested:
        requested = ["baseline", *requested]

    # Validate.
    known = set(runner_registry.all_names())
    unknown = [n for n in requested if n not in known]
    if unknown:
        p.error(f"unknown runner(s): {', '.join(unknown)}; known: {sorted(known)}")

    # Filter optional runners that the user didn't opt into via flag.
    filtered: list[str] = []
    for n in requested:
        if runner_registry.is_optional(n) and n not in opt_ins:
            continue
        filtered.append(n)

    return Args(
        pdf_dir=ns.pdf_dir.expanduser(),
        out_dir=ns.out_dir.expanduser(),
        runners=filtered,
        pages_per_pdf=int(ns.pages_per_pdf),
        seed=int(ns.seed),
        max_pdfs=ns.max_pdfs,
        include_glob=ns.include_glob,
    )


def _maxrss_mb() -> float:
    # ru_maxrss is bytes on macOS, KB on Linux. We're macOS-first; document
    # the platform mismatch rather than paper over it.
    val = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return val / (1024.0 * 1024.0)
    return val / 1024.0


def _list_pdfs(pdf_dir: Path, glob: str, cap: int | None) -> list[Path]:
    pdfs = sorted(p for p in pdf_dir.glob(glob) if p.is_file())
    return pdfs[:cap] if cap is not None else pdfs


def _sample_pages(n_pages: int, pages_per_pdf: int, rng: random.Random) -> list[int]:
    if pages_per_pdf <= 0 or pages_per_pdf >= n_pages:
        return list(range(n_pages))
    return sorted(rng.sample(range(n_pages), pages_per_pdf))


def _page_count(pdf_path: Path) -> int:
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:
        return 0
    try:
        return doc.page_count
    finally:
        doc.close()


def _md_path(out_dir: Path, pdf: Path, page: int, runner: str) -> Path:
    return out_dir / "by_pdf" / pdf.stem / str(page) / f"{runner}.md"


def _write_md(p: Path, md: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md, encoding="utf-8")


def _row(
    pdf: Path,
    page: int,
    runner: str,
    result: RunnerResult,
    md_path: Path,
    out_dir: Path,
) -> dict[str, object]:
    base = result.to_row()
    base["pdf"] = pdf.name
    base["page"] = page
    base["runner"] = runner
    base["output_md_path"] = str(md_path.relative_to(out_dir))
    # Keep only declared columns; flatten anything else into output but
    # don't widen the schema implicitly.
    return {k: base.get(k, "") for k in CSV_COLUMNS}


def _summarize(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Aggregate per (pdf, runner)."""
    by_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    for r in rows:
        by_key.setdefault((str(r["pdf"]), str(r["runner"])), []).append(r)

    summary: list[dict[str, object]] = []
    for (pdf, runner), group in sorted(by_key.items()):
        walls = [float(r["wall_ms"]) for r in group if not r["crashed"]]
        jaccards = [float(r["token_jaccard"]) for r in group if not r["crashed"]]
        summary.append(
            {
                "pdf": pdf,
                "runner": runner,
                "n_pages": len(group),
                "n_crashes": sum(1 for r in group if r["crashed"]),
                "total_wall_ms": round(sum(walls), 2),
                "median_wall_ms": round(statistics.median(walls), 2) if walls else 0.0,
                "p95_wall_ms": round(_p95(walls), 2),
                "mean_jaccard": round(statistics.fmean(jaccards), 4) if jaccards else 0.0,
                "n_h1_sum": sum(int(r["n_h1"]) for r in group),
                "n_h2_sum": sum(int(r["n_h2"]) for r in group),
                "n_h3_sum": sum(int(r["n_h3"]) for r in group),
                "n_tables_sum": sum(int(r["n_tables"]) for r in group),
                "n_list_items_sum": sum(int(r["n_list_items"]) for r in group),
            }
        )
    return summary


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round(0.95 * (len(s) - 1))))
    return s[k]


def _write_csv(path: Path, rows: list[dict[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(columns))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _results_md(
    out_dir: Path,
    args: Args,
    rows: list[dict[str, object]],
    summary_rows: list[dict[str, object]],
    pdfs: list[Path],
) -> None:
    template_path = Path(__file__).with_name("RESULTS_TEMPLATE.md")
    template = template_path.read_text(encoding="utf-8")

    n_pages_total = len({(r["pdf"], r["page"]) for r in rows})

    runners_used = sorted({str(r["runner"]) for r in rows})

    runner_table_lines = [
        "| runner | n_pages | n_crashes | median_wall_ms | p95_wall_ms | mean_jaccard |",
        "|---|---|---|---|---|---|",
    ]
    for runner in runners_used:
        group = [s for s in summary_rows if s["runner"] == runner]
        n_pages = sum(int(g["n_pages"]) for g in group)
        n_crashes = sum(int(g["n_crashes"]) for g in group)
        all_walls = [float(g["median_wall_ms"]) for g in group if g["median_wall_ms"]]
        all_p95 = [float(g["p95_wall_ms"]) for g in group if g["p95_wall_ms"]]
        all_jacc = [float(g["mean_jaccard"]) for g in group if g["mean_jaccard"]]
        runner_table_lines.append(
            "| {r} | {p} | {c} | {m} | {p95} | {j} |".format(
                r=runner,
                p=n_pages,
                c=n_crashes,
                m=round(statistics.fmean(all_walls), 2) if all_walls else "—",
                p95=round(statistics.fmean(all_p95), 2) if all_p95 else "—",
                j=round(statistics.fmean(all_jacc), 4) if all_jacc else "—",
            )
        )

    out = (
        template.replace("{{GENERATED_AT}}", dt.datetime.now().isoformat(timespec="seconds"))
        .replace("{{PDF_DIR}}", str(args.pdf_dir))
        .replace("{{N_PDFS}}", str(len(pdfs)))
        .replace("{{N_PAGES}}", str(n_pages_total))
        .replace("{{RUNNERS}}", ", ".join(runners_used))
        .replace("{{PAGES_PER_PDF}}", str(args.pages_per_pdf))
        .replace("{{SEED}}", str(args.seed))
        .replace("{{RUNNER_TABLE}}", "\n".join(runner_table_lines))
    )

    (out_dir / "RESULTS.md").write_text(out, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.pdf_dir.exists():
        print(f"pdf_dir does not exist: {args.pdf_dir}", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    # Seeded RNG for reproducible page sampling; not cryptographic.
    rng = random.Random(args.seed)  # noqa: S311

    runners = runner_registry.resolve(
        args.runners, opt_ins={n for n in args.runners if runner_registry.is_optional(n)}
    )

    # One-time setup per runner.
    setups: dict[str, object] = {}
    for name, mod in runners:
        try:
            setups[name] = mod.setup()
        except Exception as e:
            print(f"[setup-error] {name}: {e}", file=sys.stderr)
            return 3

    pdfs = _list_pdfs(args.pdf_dir, args.include_glob, args.max_pdfs)
    if not pdfs:
        print(f"no PDFs found under {args.pdf_dir} matching {args.include_glob!r}", file=sys.stderr)
        return 1

    runner_names = [n for n, _ in runners]
    print(
        f"[bakeoff] {len(pdfs)} pdfs × runners={runner_names}; "
        f"pages_per_pdf={args.pages_per_pdf}",
        file=sys.stderr,
    )

    # Stream metrics.csv incrementally so Ctrl+C still leaves usable
    # data on disk. We re-write summary.csv and RESULTS.md at the end.
    metrics_path = args.out_dir / "metrics.csv"
    metrics_f = metrics_path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(metrics_f, fieldnames=list(CSV_COLUMNS))
    writer.writeheader()
    metrics_f.flush()

    rows: list[dict[str, object]] = []
    t_start = time.perf_counter()
    n_completed = 0
    try:
        for pdf_idx, pdf in enumerate(pdfs, start=1):
            n_pages = _page_count(pdf)
            if n_pages == 0:
                print(f"[skip] {pdf}: 0 pages or unreadable", file=sys.stderr)
                continue
            pages = _sample_pages(n_pages, args.pages_per_pdf, rng)
            short = pdf.name if len(pdf.name) <= 40 else pdf.name[:37] + "..."

            for page in pages:
                baseline_md = ""
                for name, mod in runners:
                    try:
                        rss_before = _maxrss_mb()
                        r = mod.run(setups[name], pdf, page)
                        r.rss_delta_mb = max(0.0, _maxrss_mb() - rss_before)
                    except Exception as e:
                        r = RunnerResult(
                            wall_ms=0.0,
                            rss_delta_mb=0.0,
                            output_md="",
                            crashed=True,
                            error=f"orchestrator-caught {type(e).__name__}: {e}\n"
                            + traceback.format_exc(limit=2),
                        )
                    # Normalise spacing/lists; baseline is plain text so
                    # we skip it (mdformat would mangle the unstructured
                    # output and skew the jaccard denominator).
                    if name != "baseline":
                        r.output_md = _normalize_md(r.output_md)
                    if name == "baseline":
                        baseline_md = r.output_md
                    populate_structural_metrics(r, baseline_md=baseline_md)
                    md_path = _md_path(args.out_dir, pdf, page, name)
                    _write_md(md_path, r.output_md)
                    row = _row(pdf, page, name, r, md_path, args.out_dir)
                    rows.append(row)
                    writer.writerow(row)
                    metrics_f.flush()
                    n_completed += 1

                    elapsed = time.perf_counter() - t_start
                    status = "CRASH" if r.crashed else f"{r.wall_ms / 1000:6.2f}s"
                    print(
                        f"[{pdf_idx}/{len(pdfs)}] {short:>40} p{page:>3} "
                        f"{name:<20} {status}  elapsed={elapsed / 60:5.1f}m",
                        file=sys.stderr,
                    )
    finally:
        metrics_f.close()
        for name, mod in runners:
            td = getattr(mod, "teardown", None)
            if callable(td):
                try:
                    td(setups[name])
                except Exception as e:
                    print(f"[teardown-error] {name}: {e}", file=sys.stderr)
    summary_rows = _summarize(rows)
    _write_csv(
        args.out_dir / "summary.csv",
        summary_rows,
        columns=(
            "pdf",
            "runner",
            "n_pages",
            "n_crashes",
            "total_wall_ms",
            "median_wall_ms",
            "p95_wall_ms",
            "mean_jaccard",
            "n_h1_sum",
            "n_h2_sum",
            "n_h3_sum",
            "n_tables_sum",
            "n_list_items_sum",
        ),
    )
    _results_md(args.out_dir, args, rows, summary_rows, pdfs)

    print(f"[bakeoff] done. wrote {len(rows)} rows to {args.out_dir}", file=sys.stderr)
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
