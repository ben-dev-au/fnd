"""A metadata-only change (Finder retag) must re-index; nothing else should."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from fnd.index_runner import _should_reprocess

XATTR = "com.apple.metadata:_kMDItemUserTags"


@pytest.mark.skipif(sys.platform != "darwin", reason="xattr retag is macOS-only")
def test_retag_moves_ctime_but_not_mtime_or_birthtime(tmp_path: Path) -> None:
    """The premise the whole freshness change rests on."""
    f = tmp_path / "a.md"
    f.write_text("# Hi\n\nbody\n", encoding="utf-8")
    before = f.stat()
    time.sleep(1.1)
    subprocess.run(["xattr", "-w", XATTR, "<plist></plist>", str(f)], check=True)
    after = f.stat()
    assert int(after.st_mtime) == int(before.st_mtime)
    assert int(after.st_birthtime) == int(before.st_birthtime)
    assert int(after.st_ctime) > int(before.st_ctime)


def test_unchanged_file_is_skipped() -> None:
    """Guards against the fix over-triggering and re-indexing everything."""
    assert not _should_reprocess(prior_mtime=100, prior_ctime=200, cur_mtime=100, cur_ctime=200)


def test_content_change_reprocesses() -> None:
    assert _should_reprocess(prior_mtime=100, prior_ctime=200, cur_mtime=101, cur_ctime=201)


def test_metadata_only_change_reprocesses() -> None:
    assert _should_reprocess(prior_mtime=100, prior_ctime=200, cur_mtime=100, cur_ctime=999)


def test_absent_prior_ctime_does_not_force_reprocessing() -> None:
    """v7 indexes have no stored ctime; 0 must read as 'no information'
    rather than as a change, or upgrading re-extracts the whole corpus."""
    assert not _should_reprocess(prior_mtime=100, prior_ctime=0, cur_mtime=100, cur_ctime=500)
