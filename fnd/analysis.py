"""The text analyser the index and every query-side mirror share.

``fnd_text`` is tantivy's ``en_stem`` chain with ASCII folding before the
stemmer: simple tokeniser, tokens over 40 bytes dropped, lowercase, folded,
English Snowball. Body, heading, title and path tokens are indexed with it, so
"cremant" finds "Crémant" and "recursions" reaches a "Recursion" heading.

Highlighting, fuzzy expansion and snippet anchoring call :func:`index_token`,
which runs the same analyser in-process (about 1 µs a word), so a word lights
up on screen exactly when it matches in the index.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import Final

import tantivy

TEXT_ANALYSER: Final = "fnd_text"

# A TextAnalyzer is a Rust object borrowed mutably per call; one per thread.
_LOCAL = threading.local()


def _build() -> tantivy.TextAnalyzer:
    return (
        tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
        .filter(tantivy.Filter.remove_long(40))
        .filter(tantivy.Filter.lowercase())
        .filter(tantivy.Filter.ascii_fold())
        .filter(tantivy.Filter.stemmer("english"))
        .build()
    )


def register(index: tantivy.Index) -> tantivy.Index:
    """``index`` with :data:`TEXT_ANALYSER` registered; every open must do this."""
    index.register_tokenizer(TEXT_ANALYSER, _build())
    return index


def analyse(text: str) -> list[str]:
    """``text``'s tokens exactly as the index stores them."""
    analyser = getattr(_LOCAL, "analyser", None)
    if analyser is None:
        analyser = _LOCAL.analyser = _build()
    return analyser.analyze(text)


def fold(text: str) -> str:
    """``text`` with accents folded to ASCII and nothing else changed, for
    patterns (globs, regexes) matched against folded index tokens."""
    if text.isascii():
        return text
    folder = getattr(_LOCAL, "folder", None)
    if folder is None:
        folder = _LOCAL.folder = (
            tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.raw())
            .filter(tantivy.Filter.ascii_fold())
            .build()
        )
    tokens = folder.analyze(text)
    return tokens[0] if tokens else text


@lru_cache(maxsize=65536)
def index_token(word: str) -> str:
    """One word as the index stores it; the lowercased word if analysis drops it."""
    tokens = analyse(word)
    return "".join(tokens) if tokens else word.lower()
