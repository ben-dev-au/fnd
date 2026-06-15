"""Synonym expansion (§9e).

User-curated synonym groups — kept in a TOML file (§6) — are applied at
*query time* by rewriting matching terms into Tantivy ``(term OR syn1 OR syn2)``
disjunctions. The index never sees the expansion, so synonym edits are
free (no reindex) and synonyms can change between sessions.

Two design rules:

* Multi-word synonyms come back wrapped in double quotes so the Tantivy
  parser treats them as phrases, not three independent OR'd terms.
* Terms inside an existing quoted phrase in the user's query are NOT
  expanded — the user already asked for an exact match, and overriding
  that would surprise them.

Group entries are bidirectional: any one form expands to the rest.
"""

from __future__ import annotations

import contextlib
import functools
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True, frozen=True)
class SynonymTable:
    """Canonical lookup table built from user TOML.

    Stored as an immutable tuple of normalized groups — fully immutable (not
    just ``frozen``) so the memoised default table cannot be poisoned by an
    in-place mutation. Lookups are case-insensitive on the surface form but
    the original casing of the synonyms is preserved in the expansion output
    (so users see what they typed plus what the file declared)."""

    groups: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    @classmethod
    def from_groups(cls, raw_groups: list[list[str]]) -> SynonymTable:
        cleaned: list[tuple[str, ...]] = []
        for g in raw_groups:
            terms = tuple(t.strip() for t in g if t.strip())
            if len(terms) >= 2:
                cleaned.append(terms)
        return cls(groups=tuple(cleaned))

    def expansions_for(self, term: str) -> tuple[str, ...] | None:
        """Return every synonym in the group containing ``term``, or None
        if no group matches. Match is case-insensitive."""
        needle = term.casefold()
        for g in self.groups:
            if any(t.casefold() == needle for t in g):
                return g
        return None


def load_synonyms(path: Path) -> SynonymTable:
    """Load a synonyms TOML file. Missing files yield an empty table — the
    user opts in by creating the file."""
    if not path.exists():
        return SynonymTable()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    groups: list[list[str]] = []
    syn_section = raw.get("synonyms", {})
    if isinstance(syn_section, dict):
        for sub in syn_section.values():
            if isinstance(sub, dict):
                section_groups = sub.get("groups", [])
                if isinstance(section_groups, list):
                    for g in section_groups:
                        if isinstance(g, list):
                            groups.append([str(x) for x in g])
    return SynonymTable.from_groups(groups)


# Bundled curated default table (security/tech acronyms). Lives beside the
# module so it ships in the wheel without packaging gymnastics.
DEFAULT_SYNONYMS_PATH = Path(__file__).parent / "data" / "synonyms_default.toml"


def merge_tables(*tables: SynonymTable) -> SynonymTable:
    """Combine tables, unioning any groups that share a term (case-insensitive).

    A personal group that names an existing term folds into that group rather
    than competing with it, so the user always *extends* the defaults. Earlier
    tables seed group order; later ones append their new forms."""
    comps: list[list[str]] = []
    for table in tables:
        for g in table.groups:
            keys = {t.casefold() for t in g}
            overlap = [c for c in comps if any(t.casefold() in keys for t in c)]
            if overlap:
                merged: list[str] = []
                seen: set[str] = set()
                for src in (*overlap, list(g)):
                    for t in src:
                        if t.casefold() not in seen:
                            seen.add(t.casefold())
                            merged.append(t)
                for c in overlap:
                    comps.remove(c)
                comps.append(merged)
            else:
                comps.append(list(g))
    return SynonymTable.from_groups(comps)


@functools.cache
def load_default_synonyms() -> SynonymTable:
    """The bundled curated table, merged with generated number<->word groups.

    Numbers ship on by default alongside the curated acronyms; ``expand``
    still leaves quoted terms literal, so ``"4"`` never expands.

    Memoised: the inputs (bundled TOML + generated number groups) are static
    for the process lifetime, so the disk read and ``merge_tables`` run once."""
    # Lazy import: number_synonyms imports SynonymTable from this module.
    from fnd.number_synonyms import build_number_table

    return merge_tables(load_synonyms(DEFAULT_SYNONYMS_PATH), build_number_table())


def load_merged_synonyms(personal_path: Path | None = None) -> SynonymTable:
    """Bundled defaults merged with the user's optional personal table.

    Missing personal file is fine (defaults still apply); user groups extend
    or fold into the defaults via :func:`merge_tables`. A malformed personal
    file is skipped (bundled defaults are preserved, never discarded)."""
    tables = [load_default_synonyms()]
    if personal_path is not None:
        # Invalid personal TOML is skipped so the bundled defaults survive.
        with contextlib.suppress(Exception):
            tables.append(load_synonyms(personal_path))
    return merge_tables(*tables)


def expand(query: str, table: SynonymTable) -> str:
    """Rewrite ``query`` so any synonym-group member becomes a Tantivy
    OR-disjunction over every member of that group.

    Matches single words, multi-word phrases (hyphen/space-agnostic, so both
    ``multi-factor authentication`` and ``multi factor authentication`` expand),
    and whole quoted phrases. Words inside a user's quoted phrase are not
    expanded individually — that span is already an exact-match request. The
    original term stays in the disjunction so an exact hit still scores.
    """
    if not table.groups:
        return query

    # Group lookup keyed by the \w+ token tuple (hyphens are separators) so a
    # query form matches a table form regardless of hyphenation. O(1) lookups.
    key2group: dict[tuple[str, ...], tuple[str, ...]] = {}
    max_len = 1
    for g in table.groups:
        for term in g:
            toks = tuple(re.findall(r"\w+", term.casefold()))
            if toks:
                key2group.setdefault(toks, g)
                max_len = max(max_len, len(toks))

    quoted = list(re.finditer(r'"([^"]*)"', query))
    qranges = [(m.start(), m.end()) for m in quoted]

    def in_quote(pos: int) -> bool:
        return any(s <= pos < e for s, e in qranges)

    # Replacements as (start, end, text). Quoted-phrase and bare-word spans
    # never overlap (bare words inside quotes are skipped).
    repls: list[tuple[int, int, str]] = []
    for m in quoted:
        # Token-tuple lookup (not exact string) so a quoted phrase expands
        # regardless of hyphen/space, matching the bare-word path below. A
        # single quoted token (e.g. "4", "mfa") is left literal — quoting one
        # word is the clearest exact-match request, so it never expands; only
        # genuine multi-word phrases ("multi factor authentication") do.
        key = tuple(re.findall(r"\w+", m.group(1).casefold()))
        exp = key2group.get(key) if len(key) > 1 else None
        if exp is not None:
            repls.append((m.start(), m.end(), _format_disjunction(m.group(1), exp)))

    words = [m for m in re.finditer(r"\w+", query) if not in_quote(m.start())]
    i, n = 0, len(words)
    while i < n:
        matched = False
        for k in range(min(max_len, n - i), 0, -1):
            run = words[i : i + k]
            # Contiguous phrase: only whitespace/hyphens between the tokens.
            if any(
                set(query[run[j].end() : run[j + 1].start()]) - {" ", "\t", "-"}
                for j in range(k - 1)
            ):
                continue
            grp = key2group.get(tuple(w.group(0).casefold() for w in run))
            if grp is not None:
                surface = query[run[0].start() : run[-1].end()]
                repls.append((run[0].start(), run[-1].end(), _format_disjunction(surface, grp)))
                i += k
                matched = True
                break
        if not matched:
            i += 1

    repls.sort()
    out: list[str] = []
    last = 0
    for s, e, rep in repls:
        if s < last:
            continue
        out.append(query[last:s])
        out.append(rep)
        last = e
    out.append(query[last:])
    return "".join(out)


def _format_disjunction(original: str, group: tuple[str, ...]) -> str:
    """Build ``(original OR alt1 OR alt2 …)``. Multi-word alternatives are
    wrapped in quotes so the Tantivy parser keeps them as phrases."""
    seen: set[str] = set()
    parts: list[str] = []
    for term in (original, *group):
        if term.casefold() in seen:
            continue
        seen.add(term.casefold())
        if " " in term:
            parts.append(f'"{term}"')
        else:
            parts.append(term)
    return "(" + " OR ".join(parts) + ")"
