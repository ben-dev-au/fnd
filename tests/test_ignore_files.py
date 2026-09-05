"""The ignore matcher, held to git's own answer.

``git check-ignore`` is the oracle. It is pinned away from the developer's
global excludes and ``.git/info/exclude`` — without that, a machine with a
``~/.gitignore_global`` grades against a different rulebook than CI does.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fnd.ignore_files import IgnoreStack, load_ignore_file, parse_patterns

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0,
    reason="git is the oracle for these tests",
)


def _init_repo(root: Path) -> dict[str, str]:
    """A repo whose only ignore source is the files we write."""
    env = {
        "GIT_CONFIG_GLOBAL": str(root / "no-global-config"),
        "GIT_CONFIG_SYSTEM": str(root / "no-system-config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(root),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env, cwd=str(root))
    (root / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    return env


def _git_ignores(root: Path, env: dict[str, str], rel: str) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", rel],
        cwd=str(root),
        env=env,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _ours_ignores(root: Path, rel: str) -> bool:
    """Walk the stack down to ``rel`` exactly as the walker would.

    A directory decided ignored is never descended into, so nothing beneath it
    can be re-included — git's rule, and the reason this cannot just test the
    leaf in isolation.
    """
    stack = IgnoreStack()
    current = root
    stack = stack.push(*(load_ignore_file(current, n) for n in (".gitignore", ".fndignore")))
    parts = Path(rel).parts
    for depth, part in enumerate(parts):
        current = current / part
        is_last = depth == len(parts) - 1
        is_dir = current.is_dir()
        if stack.ignored(current, is_dir=is_dir):
            return True
        if is_last:
            return False
        stack = stack.push(*(load_ignore_file(current, n) for n in (".gitignore", ".fndignore")))
    return False


def _make(root: Path, rel: str, *, is_dir: bool = False) -> None:
    target = root / rel
    if is_dir:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


# (ignore-file contents, path, is_dir)
CASES: list[tuple[str, str, bool]] = [
    ("*.pdf\n", "a.pdf", False),
    ("*.pdf\n", "sub/a.pdf", False),
    ("*.pdf\n", "a.md", False),
    ("/a.pdf\n", "a.pdf", False),
    ("/a.pdf\n", "sub/a.pdf", False),
    ("sub/a.pdf\n", "sub/a.pdf", False),
    ("sub/a.pdf\n", "other/sub/a.pdf", False),
    ("build/\n", "build", True),
    # A dir-only pattern must not match a *file* of the same name; without a
    # discriminating case here, dropping the dir_only check goes unnoticed.
    ("build/\n", "build", False),
    ("logs/\n", "logs", False),
    ("build/\n", "build/out.md", False),
    ("build\n", "build", True),
    ("*.log\n!keep.log\n", "keep.log", False),
    ("*.log\n!keep.log\n", "drop.log", False),
    ("**/tmp\n", "a/b/tmp", True),
    ("a/**/z\n", "a/b/c/z", False),
    ("a/**/z\n", "a/z", False),
    ("doc?.md\n", "doc1.md", False),
    ("doc[0-9].md\n", "doc5.md", False),
    ("doc[0-9].md\n", "docx.md", False),
    ("# comment\n*.tmp\n", "a.tmp", False),
    ("\n\n*.tmp\n", "a.tmp", False),
    ("out/**\n", "out/deep/f.md", False),
    ("!*.md\n", "a.md", False),
    ("lecture slides/\n", "lecture slides/w1.pdf", False),
]


@pytest.mark.parametrize(("contents", "rel", "is_dir"), CASES)
def test_matches_git(tmp_path: Path, contents: str, rel: str, is_dir: bool) -> None:
    env = _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(contents, encoding="utf-8")
    _make(tmp_path, rel, is_dir=is_dir)
    assert _ours_ignores(tmp_path, rel) == _git_ignores(tmp_path, env, rel), (
        f"pattern {contents!r} path {rel!r}"
    )


def test_nested_child_overrides_parent(tmp_path: Path) -> None:
    env = _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("*.md\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("!keep.md\n", encoding="utf-8")
    _make(tmp_path, "sub/keep.md")
    _make(tmp_path, "sub/drop.md")
    for rel in ("sub/keep.md", "sub/drop.md"):
        assert _ours_ignores(tmp_path, rel) == _git_ignores(tmp_path, env, rel), rel


def test_negation_cannot_resurrect_inside_an_ignored_directory(tmp_path: Path) -> None:
    """Git never lists an excluded directory, so a rule below it is inert."""
    env = _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n!build/keep.md\n", encoding="utf-8")
    _make(tmp_path, "build/keep.md")
    assert _ours_ignores(tmp_path, "build/keep.md") is True
    assert _git_ignores(tmp_path, env, "build/keep.md") is True


def test_fndignore_is_honoured_alongside_gitignore(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".fndignore").write_text("*.md\n", encoding="utf-8")
    _make(tmp_path, "a.md")
    assert _ours_ignores(tmp_path, "a.md") is True


def test_unreadable_ignore_file_degrades_to_no_rules(tmp_path: Path) -> None:
    assert load_ignore_file(tmp_path, ".gitignore") is None


def test_blank_and_comment_only_file_yields_no_patterns() -> None:
    assert parse_patterns("\n# just a comment\n   \n") == ()


_SEGMENT = st.sampled_from(["a", "b", "sub", "docs", "x.md", "y.pdf", "note.txt"])
_PATTERN = st.sampled_from(
    [
        "*.md",
        "*.pdf",
        "/x.md",
        "sub/",
        "sub/x.md",
        "**/y.pdf",
        "!x.md",
        "doc?.md",
        "a/**/x.md",
        "b",
        "*.md\n!sub/x.md",
        "sub/**",
    ]
)


@settings(
    max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(pattern=_PATTERN, parts=st.lists(_SEGMENT, min_size=1, max_size=3))
def test_agrees_with_git_on_generated_cases(
    tmp_path_factory: pytest.TempPathFactory, pattern: str, parts: list[str]
) -> None:
    root = tmp_path_factory.mktemp("repo")
    env = _init_repo(root)
    (root / ".gitignore").write_text(pattern + "\n", encoding="utf-8")
    rel = "/".join(parts)
    is_dir = "." not in parts[-1]
    _make(root, rel, is_dir=is_dir)
    assert _ours_ignores(root, rel) == _git_ignores(root, env, rel), (
        f"pattern={pattern!r} rel={rel!r} is_dir={is_dir}"
    )


class TestPathologicalPatterns:
    """Ignore files come from arbitrary cloned repositories.

    Each ``*`` compiles to an unbounded group, so a run of them backtracks
    exponentially and would hang the scan rather than fail it.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            "*" * 60 + "x",
            "**/" * 40 + "x",
            "a" + "*/" * 30 + "b",
            "[" * 50,
            "\\" * 40,
            "*" * 20 + "/" + "*" * 20 + "/x",
        ],
    )
    def test_matching_is_not_exponential(self, pattern: str) -> None:
        victim = "a/" * 40 + "file.md"
        patterns = parse_patterns(pattern + "\n")
        start = time.perf_counter()
        for p in patterns:
            p.regex.match(victim)
        assert time.perf_counter() - start < 1.0, f"{pattern[:24]!r} backtracks"

    @pytest.mark.parametrize(
        "line", ["/", "!", "!!", "\\", "   ", "#", "\\#literal", "a\\ ", "[!a-z].md", "[]].md"]
    )
    def test_degenerate_lines_do_not_raise(self, line: str) -> None:
        parse_patterns(line + "\n")

    def test_a_repeated_globstar_means_the_same_as_one(self, tmp_path: Path) -> None:
        env = _init_repo(tmp_path)
        (tmp_path / ".gitignore").write_text("**/**/x.md\n", encoding="utf-8")
        _make(tmp_path, "a/b/x.md")
        assert _ours_ignores(tmp_path, "a/b/x.md") == _git_ignores(tmp_path, env, "a/b/x.md")


class TestScopeIsTheSourceDownwards:
    """An ignore file above the source root does not govern it."""

    def test_a_repository_enclosing_the_source_does_not_empty_it(self, tmp_path: Path) -> None:
        """A dotfiles repo in the home directory — ``*`` plus a few negations,
        a common shape — otherwise makes every file under ~/Documents ignored
        and the source indexes nothing at all."""
        import subprocess

        from fnd.config import Config
        from fnd.walk import walk_sources

        home = tmp_path / "home"
        home.mkdir()
        subprocess.run(["git", "init", "-q", str(home)], check=True)
        (home / ".gitignore").write_text("*\n!.vimrc\n", encoding="utf-8")
        docs = home / "Documents"
        docs.mkdir()
        for name in ("notes.md", "paper.pdf", "todo.txt"):
            (docs / name).write_text("content", encoding="utf-8")

        cfg = Config.model_validate({"collections": {"c": {"sources": [{"path": str(docs)}]}}})
        got = {p.name for p in walk_sources(sources=cfg.collections["c"].sources)}
        assert got == {"notes.md", "paper.pdf", "todo.txt"}

    def test_an_ignore_file_inside_the_source_still_applies(self, tmp_path: Path) -> None:
        from fnd.config import Config
        from fnd.walk import walk_sources

        (tmp_path / ".gitignore").write_text("*.pdf\n", encoding="utf-8")
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        (tmp_path / "b.pdf").write_text("x", encoding="utf-8")
        cfg = Config.model_validate({"collections": {"c": {"sources": [{"path": str(tmp_path)}]}}})
        got = {p.name for p in walk_sources(sources=cfg.collections["c"].sources)}
        assert got == {"a.md"}


class TestCaseFollowsGit:
    """Asserted differentially. git takes case from the filesystem
    (``core.ignorecase``, set at init time), so asserting fnd's own answer
    would pass on a case-folding volume and on a case-sensitive one, while
    only one of them agrees with git. The oracle suite cannot see this
    either: its generated vocabulary is all lowercase."""

    def test_fnd_and_git_agree_on_a_case_mismatched_pattern(self, tmp_path: Path) -> None:
        from fnd.config import Config
        from fnd.walk import walk_sources

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / ".gitignore").write_text("README.md\nBuild/\n", encoding="utf-8")
        (tmp_path / "readme.md").write_text("x", encoding="utf-8")
        (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "x.md").write_text("x", encoding="utf-8")

        cfg = Config.model_validate({"collections": {"c": {"sources": [{"path": str(tmp_path)}]}}})
        ours = {
            str(p.relative_to(tmp_path)) for p in walk_sources(sources=cfg.collections["c"].sources)
        }
        theirs = {
            rel
            for rel in ("readme.md", "keep.txt", "build/x.md")
            if subprocess.run(
                ["git", "-C", str(tmp_path), "check-ignore", rel], capture_output=True
            ).returncode
            != 0
        }
        assert ours == theirs

    @pytest.mark.parametrize(
        ("pattern", "rel"),
        [
            ("[Bb]in/x.md", "bin/x.md"),
            ("*.[CH]", "x.c"),
            ("*.[ch]", "x.c"),
            ("doc[0-9].md", "doc3.md"),
            ("[MN]ake.md", "make.md"),
            ("*.MD", "note.md"),
            ("Build/", "build/x.md"),
        ],
    )
    def test_a_character_class_is_not_folded(self, tmp_path: Path, pattern: str, rel: str) -> None:
        """git lowercases the text but compares a class member literally, so
        ``*.[CH]`` does NOT match ``x.c``. Compiling with ``re.IGNORECASE``
        folds both sides and silently drops files git keeps.

        Only a file the walker can index is a valid probe: one with no suffix
        is skipped for its kind, which reads as "ignored" and hides a
        mismatch — that flaw cost a false result while writing this.
        """
        from fnd.config import Config
        from fnd.walk import walk_sources

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / ".gitignore").write_text(pattern + "\n", encoding="utf-8")
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

        cfg = Config.model_validate({"collections": {"c": {"sources": [{"path": str(tmp_path)}]}}})
        kept = {
            str(p.relative_to(tmp_path)) for p in walk_sources(sources=cfg.collections["c"].sources)
        }
        git_ignores = (
            subprocess.run(
                ["git", "-C", str(tmp_path), "check-ignore", rel], capture_output=True
            ).returncode
            == 0
        )
        assert (rel not in kept) == git_ignores
