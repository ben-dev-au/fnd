"""Verify DoclingDaemon lifecycle and fallback failure modes.

Requirements covered:
- F10: docling daemon spawned lazily, reused across the reindex,
       torn down at exit.
- NF4: docling fallback failure (crash, timeout, missing) falls back
       to pymupdf4llm output silently; never propagates.

The daemon tests don't actually spawn docling (would require the
docling-slim tool venv and ~3s of model load per test). They patch
the lifecycle hooks to drive the singleton machinery without touching
a real subprocess.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pymupdf  # type: ignore[import-not-found]
import pytest


@pytest.fixture
def _reset_daemon_singleton() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Ensure the DoclingDaemon singleton is reset before each test."""
    from fnd.extract import _docling_daemon

    _docling_daemon.DoclingDaemon._instance = None
    yield
    _docling_daemon.DoclingDaemon._instance = None


@pytest.mark.usefixtures("_reset_daemon_singleton")
def test_daemon_singleton_returns_none_when_docling_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10/NF4: when docling isn't on PATH, .get() returns None and
    callers fall through. No subprocess spawn attempted."""
    from fnd.extract import _docling_daemon

    monkeypatch.setattr(_docling_daemon, "_docling_python", lambda: None)
    result = _docling_daemon.DoclingDaemon.get()
    assert result is None


@pytest.mark.usefixtures("_reset_daemon_singleton")
def test_daemon_singleton_reuses_one_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: repeated .get() calls return the same singleton — model
    loads exactly once per process."""
    from fnd.extract import _docling_daemon

    fake_python = Path("/fake/python")
    monkeypatch.setattr(_docling_daemon, "_docling_python", lambda: fake_python)
    spawn_mock = MagicMock(return_value=MagicMock(spec=["poll", "stdin", "stdout", "wait", "kill"]))
    spawn_mock.return_value.poll.return_value = None  # process is alive
    monkeypatch.setattr(_docling_daemon, "_spawn_helper", spawn_mock)

    first = _docling_daemon.DoclingDaemon.get()
    second = _docling_daemon.DoclingDaemon.get()
    third = _docling_daemon.DoclingDaemon.get()
    assert first is second is third
    # Spawn was called exactly once across three .get() calls.
    assert spawn_mock.call_count == 1


@pytest.mark.usefixtures("_reset_daemon_singleton")
def test_daemon_shutdown_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: shutdown() is safe to call multiple times (atexit + explicit
    teardown shouldn't double-fault)."""
    from fnd.extract import _docling_daemon

    fake_python = Path("/fake/python")
    monkeypatch.setattr(_docling_daemon, "_docling_python", lambda: fake_python)
    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(_docling_daemon, "_spawn_helper", lambda *_a, **_kw: proc)

    _docling_daemon.DoclingDaemon.get()
    _docling_daemon.DoclingDaemon.shutdown()
    _docling_daemon.DoclingDaemon.shutdown()  # second call mustn't raise
    _docling_daemon.DoclingDaemon.shutdown()


@pytest.mark.usefixtures("_reset_daemon_singleton")
def test_fallback_returns_empty_when_daemon_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF4: when DoclingDaemon.get() returns None (docling missing),
    _try_docling_fallback returns "" — caller keeps pymupdf4llm output."""
    from fnd.extract import _docling_daemon, pdf

    monkeypatch.setattr(_docling_daemon, "_docling_python", lambda: None)
    result = pdf._try_docling_fallback("/nonexistent.pdf", 0)
    assert result == ""


@pytest.mark.usefixtures("_reset_daemon_singleton")
def test_fallback_returns_empty_when_daemon_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF4: daemon throwing mid-extraction → fallback returns "" and
    extraction continues with pymupdf4llm output. No exception leaks."""
    from fnd.extract import _docling_daemon, pdf

    fake_daemon = MagicMock()
    fake_daemon.extract_page.side_effect = RuntimeError("docling exploded")
    monkeypatch.setattr(_docling_daemon.DoclingDaemon, "get", classmethod(lambda _cls: fake_daemon))

    result = pdf._try_docling_fallback("/some.pdf", 0)
    assert result == ""


@pytest.mark.usefixtures("_reset_daemon_singleton")
def test_fallback_returns_daemon_output_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF4 happy path: when daemon returns markdown, fallback passes
    it through verbatim."""
    from fnd.extract import _docling_daemon, pdf

    fake_daemon = MagicMock()
    fake_daemon.extract_page.return_value = "## Recovered\n\n| col | val |\n|---|---|"
    monkeypatch.setattr(_docling_daemon.DoclingDaemon, "get", classmethod(lambda _cls: fake_daemon))

    result = pdf._try_docling_fallback("/some.pdf", 7)
    assert result == "## Recovered\n\n| col | val |\n|---|---|"
    fake_daemon.extract_page.assert_called_once()
    call_args = fake_daemon.extract_page.call_args
    assert call_args[0][1] == 7  # page index passed through


def test_needs_docling_fallback_triggers_on_table_label_proximity() -> None:
    """The HBR p99 case: small (sub-15%) picture region but a TABLE
    label nearby — should trigger fallback."""
    from fnd.extract import pdf

    class FakePage:
        class _Rect:
            width = 612
            height = 792

        rect = _Rect()

    md = (
        "## **TABLE 5-2**\n\n"
        "## **Forecasted revenues by distribution channel**\n\n"
        "**==> picture [324 x 70] intentionally omitted <==**\n"
    )
    assert pdf._needs_docling_fallback(cast(pymupdf.Page, FakePage()), md) is True


def test_needs_docling_fallback_skips_small_decorative_image() -> None:
    """Small image with no TABLE label nearby (typical logo/figure) →
    no fallback, pymupdf4llm output kept."""
    from fnd.extract import pdf

    class FakePage:
        class _Rect:
            width = 612
            height = 792

        rect = _Rect()

    md = (
        "Some prose paragraph here.\n\n"
        "**==> picture [80 x 40] intentionally omitted <==**\n\n"
        "More prose."
    )
    assert pdf._needs_docling_fallback(cast(pymupdf.Page, FakePage()), md) is False


def test_needs_docling_fallback_triggers_on_large_omitted_area() -> None:
    """Big picture region (>15% of page) → fallback fires regardless
    of TABLE labels (likely an image-rendered chart or borderless table)."""
    from fnd.extract import pdf

    class FakePage:
        class _Rect:
            # ~half-page picture
            width = 612
            height = 792

        rect = _Rect()

    # 400 x 200 = 80000; page area = 484704; ratio = 16.5%
    md = "**==> picture [400 x 200] intentionally omitted <==**\n"
    assert pdf._needs_docling_fallback(cast(pymupdf.Page, FakePage()), md) is True


def test_needs_docling_fallback_skips_when_no_picture_marker() -> None:
    """No picture-omitted marker at all → no fallback (the regex
    short-circuit before any area math)."""
    from fnd.extract import pdf

    class FakePage:
        class _Rect:
            width = 612
            height = 792

        rect = _Rect()

    md = "## TABLE 5-2\n\n| col | val |\n|---|---|\n| a | 1 |"
    assert pdf._needs_docling_fallback(cast(pymupdf.Page, FakePage()), md) is False
