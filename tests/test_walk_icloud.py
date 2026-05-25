"""is_dataless: detection of iCloud-offloaded placeholder files."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from fnd.walk import is_dataless


def _stat_result_with_flags(flags: int) -> os.stat_result:
    # Pass platform extras through the named-extras dict; st_flags is
    # not one of the positional 10-tuple slots on macOS.
    return os.stat_result((0,) * 10, {"st_flags": flags})


@pytest.mark.skipif(sys.platform != "darwin", reason="SF_DATALESS is macOS-only")
def test_is_dataless_true_when_flag_set(tmp_path: Path) -> None:
    p = tmp_path / "offloaded.pdf"
    p.write_bytes(b"")
    fake = _stat_result_with_flags(0x40000000)
    with patch("fnd.walk.os.stat", return_value=fake):
        assert is_dataless(p) is True


@pytest.mark.skipif(sys.platform != "darwin", reason="SF_DATALESS is macOS-only")
def test_is_dataless_false_when_flag_unset(tmp_path: Path) -> None:
    p = tmp_path / "local.pdf"
    p.write_bytes(b"")
    fake = _stat_result_with_flags(0)
    with patch("fnd.walk.os.stat", return_value=fake):
        assert is_dataless(p) is False


def test_is_dataless_false_on_stat_error(tmp_path: Path) -> None:
    """Missing file: don't raise, just say not-dataless."""
    p = tmp_path / "does-not-exist.pdf"
    assert is_dataless(p) is False
