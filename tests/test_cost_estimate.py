"""ETA calibrator tests.

Focus: signature segregation. The calibrator must average only runs
recorded under the CURRENT extractor signature so a switch from flat
to structured extraction (or back) doesn't poison the ETA with a
cohort that ran at very different speed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from fnd.tui import cost_estimate as ce


@pytest.fixture(autouse=True)
def _isolated_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Redirect the persistence path so tests never touch the real
    ``~/Library/Application Support/fnd``."""
    monkeypatch.setattr(ce, "_state_path", lambda: tmp_path / "indexer_throughput.jsonl")


def _force_signature(monkeypatch: pytest.MonkeyPatch, sig: str) -> None:
    """Pin the calibrator's view of the current extractor signature."""
    monkeypatch.setattr(ce, "_current_signature", lambda: sig)


def _write_raw_history(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write a jsonl history file directly, bypassing record_run.
    Used to seed mixed-signature or legacy (un-tagged) history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_estimate_filters_to_current_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two cohorts in history; estimate must reflect only the current
    cohort's per-PDF cost, not a blended average."""
    # Flat cohort: 1 s / PDF. Structured cohort: 10 s / PDF.
    flat_sig = "flat|cfg-abc"
    structured_sig = "pymupdf4llm-1.0|docling|cfg-abc"

    _force_signature(monkeypatch, flat_sig)
    for _ in range(3):
        ce.record_run(n_pdfs=10, cache_hits=0, cache_misses=10, elapsed_s=10.0)

    _force_signature(monkeypatch, structured_sig)
    for _ in range(3):
        ce.record_run(n_pdfs=10, cache_hits=0, cache_misses=10, elapsed_s=100.0)

    # Looking back as the flat extractor: see ~1 s/PDF (flat only).
    assert ce.estimate_per_pdf_seconds(signature=flat_sig) == pytest.approx(1.0)
    # Looking now as the structured extractor: see ~10 s/PDF only.
    assert ce.estimate_per_pdf_seconds(signature=structured_sig) == pytest.approx(10.0)

    # Default arg (None) uses the current signature.
    _force_signature(monkeypatch, structured_sig)
    assert ce.estimate_per_pdf_seconds() == pytest.approx(10.0)
    _force_signature(monkeypatch, flat_sig)
    assert ce.estimate_per_pdf_seconds() == pytest.approx(1.0)


def test_fresh_signature_returns_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signature with no recorded runs falls back to the documented
    baseline constant, not a stale cross-signature average."""
    _force_signature(monkeypatch, "flat|cfg-old")
    for _ in range(3):
        ce.record_run(n_pdfs=10, cache_hits=0, cache_misses=10, elapsed_s=10.0)

    _force_signature(monkeypatch, "pymupdf4llm-1.0|docling|cfg-new")
    assert ce.estimate_per_pdf_seconds() == ce.FALLBACK_SECONDS_PER_PDF
    assert ce.has_calibration_data() is False


def test_estimate_seconds_for_scales_with_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """estimate_seconds_for honours signature gating end-to-end."""
    _force_signature(monkeypatch, "flat|cfg-x")
    for _ in range(3):
        ce.record_run(n_pdfs=10, cache_hits=0, cache_misses=10, elapsed_s=20.0)
    # 2 s/PDF × 50 PDFs = 100 s
    assert ce.estimate_seconds_for(50) == pytest.approx(100.0)
    assert ce.estimate_seconds_for(0) == 0.0


def test_has_calibration_data_filters_by_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_signature(monkeypatch, "flat|cfg-a")
    ce.record_run(n_pdfs=10, cache_hits=0, cache_misses=10, elapsed_s=10.0)

    _force_signature(monkeypatch, "flat|cfg-a")
    assert ce.has_calibration_data() is True

    _force_signature(monkeypatch, "structured|cfg-b")
    assert ce.has_calibration_data() is False


def test_record_run_skips_tiny_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Runs with <3 PDFs are dropped to avoid setup-cost skew."""
    _force_signature(monkeypatch, "flat|cfg-a")
    ce.record_run(n_pdfs=2, cache_hits=0, cache_misses=2, elapsed_s=5.0)
    ce.record_run(n_pdfs=0, cache_hits=0, cache_misses=0, elapsed_s=5.0)
    ce.record_run(n_pdfs=10, cache_hits=0, cache_misses=10, elapsed_s=0.0)
    assert not (tmp_path / "indexer_throughput.jsonl").exists()


def test_legacy_entries_retagged_with_flat_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-signature entries (no ``signature`` field) are read as
    flat-only runs under the current cfg, so a user who indexed
    before signature tracking landed keeps their baseline."""
    legacy_sig = "flat|cfg-legacy"
    monkeypatch.setattr(ce, "_legacy_signature", lambda: legacy_sig)

    history_path = tmp_path / "indexer_throughput.jsonl"
    _write_raw_history(
        history_path,
        [
            {
                "completed_at": time.time(),
                "n_pdfs": 10,
                "cache_hits": 0,
                "cache_misses": 10,
                "elapsed_s": 5.0,
            }
            for _ in range(3)
        ],
    )

    # Future flat-only run reads back the legacy baseline.
    _force_signature(monkeypatch, legacy_sig)
    assert ce.estimate_per_pdf_seconds() == pytest.approx(0.5)
    assert ce.has_calibration_data() is True

    # A different signature still sees no matching runs.
    _force_signature(monkeypatch, "pymupdf4llm-1.0|docling|cfg-x")
    assert ce.estimate_per_pdf_seconds() == ce.FALLBACK_SECONDS_PER_PDF


def test_record_run_persists_current_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The signature field lands in the on-disk record."""
    _force_signature(monkeypatch, "flat|cfg-write-test")
    ce.record_run(n_pdfs=5, cache_hits=0, cache_misses=5, elapsed_s=5.0)
    raw = (tmp_path / "indexer_throughput.jsonl").read_text(encoding="utf-8").strip()
    parsed = json.loads(raw)
    assert parsed["signature"] == "flat|cfg-write-test"
    assert parsed["n_pdfs"] == 5
