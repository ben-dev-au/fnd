"""A user ``/regex/`` matched against words by tantivy's own regex engine.

Search runs the pattern through tantivy (a linear-time automaton, a dialect
without lookaround or back-references). Python's ``re`` backtracks: on
``/(a+)+b/`` a 24-letter word took 661 ms and every 2 more letters x4, frozen
on the event loop. So highlighting asks tantivy too: a batch of distinct stems
goes into a throwaway in-memory index and the pattern runs over it (measured
3 ms for 800 stems), with every answer cached. A pattern tantivy refuses
matches nothing, here as in search.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable

import tantivy

__all__ = ["matches", "matching"]

_FIELD = "t"
_PATTERNS_KEPT = 32
_STEMS_KEPT = 100_000
# tantivy's floor for one writer thread.
_WRITER_HEAP = 15_000_000

_lock = threading.Lock()
_answers: OrderedDict[str, dict[str, bool]] = OrderedDict()
_schema: tantivy.Schema | None = None


def _stem_schema() -> tantivy.Schema:
    global _schema
    if _schema is None:
        builder = tantivy.SchemaBuilder()
        builder.add_text_field(_FIELD, stored=True, tokenizer_name="raw")
        _schema = builder.build()
    return _schema


def _run(pattern: str, stems: set[str]) -> set[str]:
    schema = _stem_schema()
    try:
        query = tantivy.Query.regex_query(schema, _FIELD, f"(?i){pattern}")
    except ValueError:
        return set()
    index = tantivy.Index(schema)
    writer = index.writer(heap_size=_WRITER_HEAP, num_threads=1)
    for stem in stems:
        writer.add_document(tantivy.Document(**{_FIELD: stem}))
    writer.commit()
    writer.wait_merging_threads()
    index.reload()
    searcher = index.searcher()
    found = searcher.search(query, limit=len(stems)).hits
    return {searcher.doc(address)[_FIELD][0] for _, address in found}


def matching(pattern: str, stems: Iterable[str]) -> set[str]:
    """The members of ``stems`` that ``pattern`` matches whole, ignoring case."""
    wanted = set(stems)
    with _lock:
        known = _answers.get(pattern)
        if known is None:
            known = _answers[pattern] = {}
            while len(_answers) > _PATTERNS_KEPT:
                _answers.popitem(last=False)
        else:
            _answers.move_to_end(pattern)
        unknown = {stem for stem in wanted if stem not in known}
        if unknown:
            if len(known) + len(unknown) > _STEMS_KEPT:
                known.clear()
                unknown = wanted
            hits = _run(pattern, unknown)
            known.update((stem, stem in hits) for stem in unknown)
        return {stem for stem in wanted if known[stem]}


def matches(pattern: str, stem: str) -> bool:
    """Whether ``pattern`` matches ``stem`` whole; prime with :func:`matching`
    first when testing many stems."""
    with _lock:
        known = _answers.get(pattern)
        if known is not None and stem in known:
            return known[stem]
    return stem in matching(pattern, (stem,))
