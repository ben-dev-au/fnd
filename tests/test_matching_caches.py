"""The stemmer and glob translator are hot enough to need caching, and pure
enough to allow it.

Proving a highlight is visible word-matches every listed result before the first
paint — measured at 130,298 ``word_matches`` calls on a real corpus — and each
one reached the Snowball stemmer. Re-measured in-process on that path, cache
live against ``__wrapped__``: 85.5ms -> 10.1ms over 551 hit texts, at 23,156
hits against 1,896 misses.

An earlier version of this docstring credited the cache with taking startup from
2.95s to 1.11s. That attribution does not hold: ~25k stem calls covered those
551 texts, so even at a startup's ~130k the cache accounts for a few hundred
milliseconds, not ~1.8s. Whatever else moved that startup figure, it was not
this decorator.

A cache is only sound while the function is a pure function of its arguments, so
these tests hold both halves: the answers must not change, and the cache must
actually be doing something. Without the second, a future refactor could drop
the decorator and every test here would still pass while the stemmer went back
to being called 130,298 times a launch.
"""

from __future__ import annotations

from fnd.matching import _stem, glob_to_regex


def test_caching_does_not_change_what_stemming_returns() -> None:
    # Includes the shapes the stemmer actually has to think about: plurals,
    # -ing/-ed, doubled consonants, and words that must come back untouched.
    words = [
        "running",
        "runs",
        "ran",
        "happily",
        "happiness",
        "cats",
        "cat",
        "flies",
        "flying",
        "generously",
        "relational",
        "relate",
        "",
        "a",
        "PostgreSQL",
        "kryptonwidget",
        "connexion",
    ]
    uncached = _stem.__wrapped__
    for word in words:
        assert _stem(word) == uncached(word), f"caching changed the stem of {word!r}"


def test_caching_does_not_change_glob_translation() -> None:
    uncached = glob_to_regex.__wrapped__
    for glob in ("a*", "*b", "a?c", "*", "?", "plain", "a*b?c", "dot.name", "a+b"):
        assert glob_to_regex(glob) == uncached(glob), f"caching changed the regex for {glob!r}"


def test_the_stem_cache_is_actually_used() -> None:
    """The point of the exercise: a repeated word must not re-enter the stemmer.

    Asserted on the cache itself rather than on elapsed time — a timing
    threshold would be flaky on a loaded machine and would not say WHY it got
    slower.
    """
    _stem.cache_clear()
    for _ in range(50):
        _stem("running")
    info = _stem.cache_info()
    assert info.misses == 1, f"expected one real stem call, got {info.misses}"
    assert info.hits == 49, f"expected 49 cache hits, got {info.hits}"


def test_the_glob_cache_is_actually_used() -> None:
    glob_to_regex.cache_clear()
    for _ in range(20):
        glob_to_regex("term*")
    info = glob_to_regex.cache_info()
    assert info.misses == 1, f"expected one real translation, got {info.misses}"
    assert info.hits == 19, f"expected 19 cache hits, got {info.hits}"
