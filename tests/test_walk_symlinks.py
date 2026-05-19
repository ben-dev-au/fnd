"""Symlink-handling tests for :func:`fnd.walk.walk`. (S5)

The two protections pinned here:

- A symlinked collection *root* is refused when ``follow_symlinks=False``,
  so a typo or a hostile config can't point fnd at ``/etc`` via a link.
- Directory symlinks inside the tree are not recursed into (we pass
  ``recurse_symlinks=False`` to ``rglob`` rather than relying on the
  Python 3.13 default).
"""

from __future__ import annotations

from pathlib import Path

from fnd.walk import walk


def test_symlinked_root_refused_by_default(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "doc.md").write_text("# hi\n", encoding="utf-8")
    link = tmp_path / "linked"
    link.symlink_to(real)

    out = list(walk(roots=[link]))
    assert out == []


def test_symlinked_root_followed_when_opted_in(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "doc.md").write_text("# hi\n", encoding="utf-8")
    link = tmp_path / "linked"
    link.symlink_to(real)

    out = list(walk(roots=[link], follow_symlinks=True))
    assert any(p.name == "doc.md" for p in out)


def test_directory_symlink_inside_root_is_not_recursed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    secret = tmp_path / "secret"
    root.mkdir()
    secret.mkdir()
    (root / "ok.md").write_text("# ok\n", encoding="utf-8")
    (secret / "leaked.md").write_text("# leaked\n", encoding="utf-8")
    (root / "shortcut").symlink_to(secret)

    out = [p.name for p in walk(roots=[root])]
    assert "ok.md" in out
    assert "leaked.md" not in out
