"""``~~`` matches a frontmatter value as main did, and ``file.path`` as the walk does.

A URL or a slashed tag is not a path: under path rules ``*`` stops at ``/``, so a
legacy rule like ``url ~~ 'https://*'`` silently stopped matching every note it
was written for, and the next update dropped them.
"""

from __future__ import annotations

from fnd.filter_dsl import compile_filter


def test_a_star_in_a_frontmatter_glob_crosses_a_slash() -> None:
    assert compile_filter("url ~~ 'https://*'")({"url": "https://example.com/a/b"})


def test_a_question_mark_in_a_frontmatter_glob_crosses_a_slash() -> None:
    assert compile_filter("topic ~~ 'web?dev'")({"topic": "web/dev"})


def test_a_path_glob_still_stops_at_a_folder() -> None:
    """``file.path`` keeps the walker's language, so it agrees with ``includes``."""
    assert not compile_filter("file.path ~~ 'notes/*.md'")({"file.path": "notes/sub/x.md"})
    assert compile_filter("file.path ~~ '**/*.md'")({"file.path": "a.md"})
