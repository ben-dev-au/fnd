"""Corrections for query words the index has never seen.

A plain query word whose analysed token is in no body chunk is respelt: every
surface word one edit away (insert, delete, substitute, or swap two neighbours)
is analysed, and the ones the index holds become the word's corrections, most
frequent first. Editing the surface word rather than the stem catches suffix
typos: "polymorphsim" is three edits from the stem "polymorph" but one swap
from "polymorphism". Fusion runs the respelt query as its own pass.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from fnd.analysis import index_token
from fnd.schema import F_BODY
from fnd.stopwords import STOPWORDS

if TYPE_CHECKING:
    from fnd.query import Searcher

_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz"
_WORD_RE: Final = re.compile(r"[^\W\d_]+")
# Below 3 letters one edit reaches too many unrelated words (matching.auto_fuzzy_distance).
_MIN_LETTERS: Final = 3
_MAX_CORRECTIONS: Final = 3
# A respelling this much rarer than the best one is noise ("recurso" beside "recursion").
_MIN_SHARE: Final = 0.05


def _one_edit(word: str) -> set[str]:
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    deletes = {a + b[1:] for a, b in splits if b}
    swaps = {a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1}
    substitutes = {a + c + b[1:] for a, b in splits if b for c in _ALPHABET}
    inserts = {a + c + b for a, b in splits for c in _ALPHABET}
    return (deletes | swaps | substitutes | inserts) - {word}


def corrections(searcher: Searcher, words: list[str]) -> dict[str, tuple[str, ...]]:
    """Each unindexed word in ``words`` mapped to its indexed respellings."""
    index = searcher._searcher
    out: dict[str, tuple[str, ...]] = {}
    for word in words:
        if not _WORD_RE.fullmatch(word) or len(word) < _MIN_LETTERS:
            continue
        if index.doc_freq(F_BODY, index_token(word)) > 0:
            continue
        edits = _one_edit(word.lower())
        # One edit from a stopword ("teh"), the likely word is one search drops.
        if edits & STOPWORDS:
            continue
        best: dict[str, tuple[int, str]] = {}
        for candidate in edits:
            token = index_token(candidate)
            freq = index.doc_freq(F_BODY, token)
            if freq and freq > best.get(token, (0, ""))[0]:
                best[token] = (freq, candidate)
        ranked = sorted(best.values(), reverse=True)[:_MAX_CORRECTIONS]
        if ranked:
            floor = ranked[0][0] * _MIN_SHARE
            out[word] = tuple(candidate for freq, candidate in ranked if freq >= floor)
    return out


def respelt(words: list[str], fixes: dict[str, tuple[str, ...]]) -> str:
    """``words`` joined, each corrected word replaced by an OR of its fixes."""
    return " ".join(_respell(w, fixes.get(w, ())) for w in words)


def _respell(word: str, options: tuple[str, ...]) -> str:
    if not options:
        return word
    return options[0] if len(options) == 1 else f"({' OR '.join(options)})"


def describe(fixes: dict[str, tuple[str, ...]]) -> str:
    """One line naming each respelling searched: ``Also searched recurson as recursion.``"""
    parts = [f"{word} as {' or '.join(options)}" for word, options in fixes.items()]
    return f"Also searched {'; '.join(parts)}."
