"""Shared corpora for the preview mount/coverage tests.

The builders here encode assumptions about ``tuning`` thresholds, so they live
in one place: a copy per test module means a threshold change has to be found
in each, and a partial edit silently weakens whichever copy was missed.
"""

from __future__ import annotations

from pathlib import Path

from fnd.index import build_index


def wide_doc(tmp_path: Path, tmp_index_dir: Path) -> Path:
    """One file that stays WINDOWED, with matches far enough apart that
    consecutive jumps land outside the mounted window.

    Both properties are load-bearing. Above ``FULLMOUNT_CHUNK_BUDGET`` (250)
    chunks the file is never full-mounted, so an out-of-window jump takes the
    fresh-container rebuild path — the one that leaked. A smaller file is
    full-mounted and every jump scrolls in place, which is why the first version
    of the reclaim test passed with the fix reverted.
    """
    notes = tmp_path / "notes"
    notes.mkdir()
    lines: list[str] = ["# Wide doc", ""]
    for section in range(320):
        lines.append(f"## Section {section}")
        # Matches every 25th section: far beyond the visible-first window, so
        # each navigation rebuilds rather than scrolling within what is mounted.
        lines.append(
            f"quartzfin marker in section {section}."
            if section % 25 == 0
            else f"Filler prose for section {section}."
        )
        lines.extend([f"More filler line {i} for section {section}." for i in range(4)])
        lines.append("")
    (notes / "wide.md").write_text("\n".join(lines), encoding="utf-8")
    build_index(roots=[notes], index_dir=tmp_index_dir, collection="notes")
    return tmp_index_dir
