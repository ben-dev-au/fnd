"""Tests for the `fnd cache` CLI surface.

Covers the user-facing wrapper around ExtractionCache:
- `cache status` reports entry count + size without crashing
- `cache clear` requires confirmation, removes the directory on yes
- `cache prune --dry-run` enumerates stale entries vs fresh
- `cache info <path>` shows the would-be cache key + HIT/MISS

Tests redirect the cache dir to a tmp path via monkeypatching so they
don't touch the dev's real `~/Library/Caches/fnd`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fnd.cache import CACHE_SCHEMA_VERSION
from fnd.cli import app


def _run(*argv: str, input: str = "") -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(app, list(argv), input=input, catch_exceptions=False)
    return result.exit_code, result.output


@pytest.fixture(autouse=True)
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:  # pyright: ignore[reportUnusedFunction]
    """Point the cache module at a per-test temp dir."""
    cache_root = tmp_path / "extraction"
    monkeypatch.setattr("fnd.cache.default_cache_dir", lambda: cache_root)
    return cache_root


def test_cache_status_when_empty() -> None:
    """Empty / non-existent cache reports clearly without crashing."""
    code, out = _run("cache", "status")
    assert code == 0
    assert "not yet created" in out


def test_cache_status_with_entries(cache_dir: Path) -> None:
    """Status reports saved-texturing count + size after a put."""
    # Write a fake entry directly
    shard = cache_dir / "ab"
    shard.mkdir(parents=True)
    entry = shard / "abcdef--testsig.json"
    entry.write_text(json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "chunks": []}))

    code, out = _run("cache", "status")
    assert code == 0
    assert "saved texturings:" in out
    assert "1" in out
    assert "disk used:" in out


def test_cache_clear_aborts_without_yes(cache_dir: Path) -> None:
    """Without --yes and answering 'n', clear must NOT remove anything."""
    shard = cache_dir / "ab"
    shard.mkdir(parents=True)
    (shard / "abcdef--testsig.json").write_text("{}")

    code, out = _run("cache", "clear", input="n\n")
    assert code != 0
    assert "aborted" in out.lower()
    assert cache_dir.exists()


def test_cache_clear_with_yes(cache_dir: Path) -> None:
    """--yes skips the prompt and removes the cache dir."""
    shard = cache_dir / "ab"
    shard.mkdir(parents=True)
    (shard / "abcdef--testsig.json").write_text("{}")

    code, out = _run("cache", "clear", "--yes")
    assert code == 0
    assert "removed" in out.lower()
    assert not cache_dir.exists()


def test_cache_prune_dry_run_lists_stale(cache_dir: Path) -> None:
    """Stale entries (different extractor signature) are reported in dry-run."""
    shard = cache_dir / "ab"
    shard.mkdir(parents=True)
    # Stale: signature won't match current.
    (shard / "abcdef--old-extractor-v0.json").write_text(
        json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "chunks": []})
    )
    code, out = _run("cache", "prune", "--dry-run")
    assert code == 0
    assert "stale entries" in out.lower()
    assert "1" in out
    # File still present (dry-run).
    assert any(shard.glob("*.json"))


def test_cache_info_for_existing_file(tmp_path: Path) -> None:
    """`fnd cache info <path>` shows the key + HIT/MISS for a real file."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.0\n...minimal\n")
    code, out = _run("cache", "info", str(f))
    assert code == 0
    assert "sha256:" in out
    assert "extractor signature:" in out
    assert "MISS" in out  # nothing in the per-test isolated cache


def test_cache_info_for_missing_file(tmp_path: Path) -> None:
    """`fnd cache info` on a non-existent path errors cleanly."""
    code, out = _run("cache", "info", str(tmp_path / "nonexistent.pdf"))
    assert code != 0
    assert "not found" in out.lower()
