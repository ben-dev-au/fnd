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

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True, frozen=True)
class SynonymTable:
    """Canonical lookup table built from user TOML.

    Stored as a list of normalized groups. Lookups are case-insensitive on
    the surface form but the original casing of the synonyms is preserved
    in the expansion output (so users see what they typed plus what the
    file declared)."""

    groups: list[tuple[str, ...]] = field(default_factory=list)

    @classmethod
    def from_groups(cls, raw_groups: list[list[str]]) -> SynonymTable:
        cleaned: list[tuple[str, ...]] = []
        for g in raw_groups:
            terms = tuple(t.strip() for t in g if t.strip())
            if len(terms) >= 2:
                cleaned.append(terms)
        return cls(groups=cleaned)

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


def load_default_synonyms() -> SynonymTable:
    """The bundled curated table. Empty if the data file is somehow absent."""
    return load_synonyms(DEFAULT_SYNONYMS_PATH)


def load_merged_synonyms(personal_path: Path | None = None) -> SynonymTable:
    """Bundled defaults merged with the user's optional personal table.

    Missing personal file is fine (defaults still apply); user groups extend
    or fold into the defaults via :func:`merge_tables`."""
    tables = [load_default_synonyms()]
    if personal_path is not None:
        tables.append(load_synonyms(personal_path))
    return merge_tables(*tables)


# Match either a quoted phrase ("..."), or a contiguous run of word chars.
# The whole-string scan rebuilds the query: phrases are emitted untouched
# (they short-circuit synonym expansion); bare words are looked up.
_TOKEN_RE = re.compile(r'"[^"]*"|\w[\w\-]*')


def expand(query: str, table: SynonymTable) -> str:
    """Rewrite ``query`` so any term in a synonym group becomes a Tantivy
    OR-disjunction over every member of that group.

    Single-word synonyms are emitted bare; multi-word synonyms are quoted
    so the parser sees a phrase. Inner words of a quoted phrase are NOT
    expanded individually — the user already asked for an exact phrase. But
    if the whole phrase itself matches a synonym group member, the phrase
    expands as a unit.

    The original term is always included in the disjunction so an exact
    match still scores normally if Tantivy finds it.
    """
    if not table.groups:
        return query

    out_parts: list[str] = []
    last = 0

    # Walk phrases and bare words in document order, picking the right
    # action for each.
    token_re = re.compile(r'"([^"]*)"|(\w[\w\-]*)')
    for m in token_re.finditer(query):
        phrase_body = m.group(1)
        word = m.group(2)
        if phrase_body is not None:
            # Whole-phrase synonym match? Expand. Otherwise leave it alone.
            expansions = table.expansions_for(phrase_body)
            if expansions is not None:
                out_parts.append(query[last : m.start()])
                out_parts.append(_format_disjunction(phrase_body, expansions))
                last = m.end()
            # else: drop through; phrase stays untouched in the output.
        elif word is not None:
            expansions = table.expansions_for(word)
            if expansions is None:
                continue
            out_parts.append(query[last : m.start()])
            out_parts.append(_format_disjunction(word, expansions))
            last = m.end()
    out_parts.append(query[last:])
    return "".join(out_parts)


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
