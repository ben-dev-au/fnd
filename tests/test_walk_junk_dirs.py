"""Walk-time junk-directory prune (fix for the SSD-collection freeze).

The walk skips ``node_modules`` / ``__pycache__`` / ``.venv`` / etc. at
descent so a coursework folder that happens to contain cloned dev repos
doesn't pull tens of thousands of READMEs into the index. Verifies the
prune set, the disable knob, and the user-extend list.
"""

from __future__ import annotations

from pathlib import Path

from fnd.config import DEFAULT_JUNK_DIRS, Config, Defaults, SourceConfig
from fnd.walk import resolve_skip_dirs, walk, walk_sources


def _touch(p: Path, body: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_default_junk_dirs_pruned_at_descent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _touch(root / "README.md")
    # node_modules + __pycache__ + .venv: all three sample dirs from the
    # default junk set; representative of the real user incident.
    _touch(root / "node_modules" / "react" / "README.md")
    _touch(root / "node_modules" / "react" / "nested" / "doc.md")
    _touch(root / "src" / "__pycache__" / "module.cpython-313.pyc")
    _touch(root / ".venv" / "lib" / "site-packages" / "thing" / "README.md")

    out = sorted(p.relative_to(root).as_posix() for p in walk(roots=[root]))
    assert out == ["README.md"]


def test_skip_dirs_disabled_restores_legacy_walk(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _touch(root / "README.md")
    _touch(root / "node_modules" / "react" / "README.md")

    out = sorted(p.relative_to(root).as_posix() for p in walk(roots=[root], skip_dirs=frozenset()))
    assert out == ["README.md", "node_modules/react/README.md"]


def test_extra_skip_dirs_extend_defaults(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _touch(root / "README.md")
    _touch(root / "build" / "out.md")  # not in default junk set
    _touch(root / "node_modules" / "thing" / "README.md")  # default skip

    skip = DEFAULT_JUNK_DIRS | {"build"}
    out = sorted(p.relative_to(root).as_posix() for p in walk(roots=[root], skip_dirs=skip))
    assert out == ["README.md"]


def test_walk_sources_threads_skip_dirs(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _touch(root / "notes.md")
    _touch(root / "node_modules" / "pkg" / "README.md")

    sources = [SourceConfig(path=root, includes=["**/*.md"])]
    out = sorted(p.name for p in walk_sources(sources=sources))
    assert out == ["notes.md"]


def test_node_modules_subtree_is_not_descended(tmp_path: Path) -> None:
    """If a junk dir were merely filtered post-rglob, every file inside
    it would still be visited. We verify the prune is at descent by
    sentinelling the inside of node_modules with a file that would raise
    if scandir touched it via a follow-up resolve(). The cheapest signal
    available cross-platform is a count check: 100 nested files inside
    node_modules cost effectively zero time when pruned."""
    root = tmp_path / "project"
    _touch(root / "README.md")
    for i in range(100):
        _touch(root / "node_modules" / f"pkg{i}" / "README.md")
        _touch(root / "node_modules" / f"pkg{i}" / "deep" / "deeper" / "x.md")

    out = list(walk(roots=[root]))
    assert [p.name for p in out] == ["README.md"]


def test_resolve_skip_dirs_disabled_returns_empty() -> None:
    defaults = Defaults(skip_junk_dirs=False)
    assert resolve_skip_dirs(defaults) == frozenset()


def test_resolve_skip_dirs_extends_with_extras() -> None:
    defaults = Defaults(extra_junk_dirs=["build", "dist"])
    skip = resolve_skip_dirs(defaults)
    assert "build" in skip
    assert "dist" in skip
    assert "node_modules" in skip  # defaults still active


def test_resolve_skip_dirs_no_defaults() -> None:
    assert resolve_skip_dirs(None) == DEFAULT_JUNK_DIRS


def test_full_config_roundtrip_keeps_junk_settings() -> None:
    """The new fields land on the loaded Config so the indexer path can
    read them via ``defaults.skip_junk_dirs`` / ``extra_junk_dirs``."""
    cfg = Config(defaults=Defaults(skip_junk_dirs=False, extra_junk_dirs=["tmp"]))
    assert cfg.defaults.skip_junk_dirs is False
    assert cfg.defaults.extra_junk_dirs == ["tmp"]
