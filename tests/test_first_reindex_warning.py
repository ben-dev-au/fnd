"""First-reindex warning behaviour tests (F21).

Tests cover the marker-file lifecycle, ETA estimator math, and the
helpers that the FNDApp uses to decide whether to show the warning.
The Textual modal class itself is exercised indirectly — full pilot
testing of modals is flaky under load and out of scope for the
phase 2.5 work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fnd.config import CollectionConfig, SourceConfig
from fnd.tui.first_reindex_warning import (
    count_pdfs,
    estimate_eta_seconds,
    fmt_duration,
    has_been_seen,
    mark_seen,
    reset_seen,
)


@pytest.fixture(autouse=True)
def _isolated_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Redirect the marker path to a tmp dir so tests don't touch
    the real ``~/Library/Application Support/fnd``."""
    monkeypatch.setattr(
        "fnd.tui.first_reindex_warning._marker_path",
        lambda: tmp_path / "first_reindex_warning_seen",
    )


def test_marker_initially_unseen() -> None:
    assert has_been_seen() is False


def test_mark_seen_persists() -> None:
    mark_seen()
    assert has_been_seen() is True


def test_reset_seen_clears_marker() -> None:
    mark_seen()
    assert has_been_seen() is True
    reset_seen()
    assert has_been_seen() is False


def test_reset_seen_safe_when_marker_absent() -> None:
    """No exception when reset is called and there's nothing to reset."""
    reset_seen()
    reset_seen()  # idempotent
    assert has_been_seen() is False


def test_count_pdfs_finds_fixture() -> None:
    """count_pdfs walks the source's filter chain like a real reindex."""
    fixtures = Path(__file__).parent / "fixtures" / "papers"
    cfg = CollectionConfig(sources=[SourceConfig(path=fixtures)])
    n = count_pdfs(cfg)
    assert n >= 1


def test_count_pdfs_empty_collection() -> None:
    """No sources → zero PDFs (warning would be skipped)."""
    cfg = CollectionConfig(sources=[])
    assert count_pdfs(cfg) == 0


def test_estimate_eta_seconds_is_linear_in_count() -> None:
    """100 PDFs takes ~30 × 100 = 3000s; 500 PDFs is 5× that."""
    eta_100 = estimate_eta_seconds(100)
    eta_500 = estimate_eta_seconds(500)
    assert eta_500 == pytest.approx(eta_100 * 5)


def test_fmt_duration_under_minute() -> None:
    assert fmt_duration(30) == "30s"


def test_fmt_duration_minutes() -> None:
    s = fmt_duration(3000)
    assert "min" in s
    assert "50" in s


def test_fmt_duration_hours() -> None:
    s = fmt_duration(7200 + 600)  # 2h 10m
    assert "h" in s
    assert "2" in s
