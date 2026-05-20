"""Shape-only smoke test for the PDF bake-off harness.

This test exists to catch metric-schema drift, not to validate
extraction quality. It runs the baseline runner against the one
checked-in PDF and asserts the CSV has the expected columns and a
plausible row count.
"""

from __future__ import annotations

import csv
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tools.pdf_bakeoff.cli import main
from tools.pdf_bakeoff.metrics import CSV_COLUMNS

FIXTURE = Path(__file__).parent / "fixtures" / "papers" / "test.pdf"


@pytest.fixture
def out_dir(tmp_path: Path) -> Iterator[Path]:
    out = tmp_path / "out"
    yield out
    shutil.rmtree(out, ignore_errors=True)


def test_baseline_runner_produces_expected_csv_shape(out_dir: Path) -> None:
    assert FIXTURE.exists(), f"missing fixture: {FIXTURE}"

    rc = main(
        [
            str(FIXTURE.parent),
            str(out_dir),
            "--runners",
            "baseline",
            "--pages-per-pdf",
            "0",
            "--include-glob",
            "test.pdf",
        ]
    )
    assert rc == 0

    metrics = out_dir / "metrics.csv"
    summary = out_dir / "summary.csv"
    results = out_dir / "RESULTS.md"
    assert metrics.exists()
    assert summary.exists()
    assert results.exists()

    with metrics.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert tuple(reader.fieldnames or ()) == CSV_COLUMNS
        rows = list(reader)

    assert len(rows) >= 1, "expected at least one (pdf, page, baseline) row"
    for r in rows:
        assert r["runner"] == "baseline"
        assert r["pdf"].endswith(".pdf")
        assert r["crashed"] in ("False", "false", "0")
        # baseline jaccard against itself is exactly 1.0
        assert float(r["token_jaccard"]) == pytest.approx(1.0)


def test_baseline_writes_per_page_markdown(out_dir: Path) -> None:
    rc = main(
        [
            str(FIXTURE.parent),
            str(out_dir),
            "--runners",
            "baseline",
            "--pages-per-pdf",
            "0",
            "--include-glob",
            "test.pdf",
        ]
    )
    assert rc == 0

    by_pdf = out_dir / "by_pdf" / FIXTURE.stem
    assert by_pdf.exists()
    page_dirs = sorted(by_pdf.iterdir())
    assert page_dirs, "no per-page directories were written"
    for d in page_dirs:
        assert (d / "baseline.md").exists()
