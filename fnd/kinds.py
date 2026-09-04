"""Central registry of the file types fnd can index — the single source of truth.

Every file type is one :class:`KindSpec`: a stable ``id`` (the value stored in
the index ``F_KIND`` field), the suffixes that map to it, the extractor module
that parses it, and the display :class:`Category` it belongs to for the Filters
tree and the source-creation wizard.

Consumers (walk, dispatch, the preview router, config/apps validation, the CLI,
the Filters panel) derive their lookups from here instead of hard-coding kind
lists, so adding a file type is one row in ``KIND_SPECS`` — plus, for a
brand-new format, a matching ``fnd/extract/<module>.py``.

``kind`` is stored fine-grained (one id per format); ``category`` is a *grouping*
concept only and is **not** stored in the index — a category filter expands to
its member kind ids. This module imports nothing from ``fnd`` so it is safe to
import from anywhere without an import cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Category:
    """A display grouping of kinds, shown as a branch in the Filters tree."""

    id: str
    label: str
    order: int


@dataclass(frozen=True, slots=True)
class KindSpec:
    """One indexable file type."""

    id: str  # F_KIND value, e.g. "python", "pdf"
    label: str  # UI label, e.g. "Python", "PDF"
    suffixes: tuple[str, ...]  # lowercase, dot-prefixed
    extractor_module: str  # module under fnd.extract exposing extract()
    category: str  # Category.id this kind belongs to
    markdown_rendered: bool  # populates body_md → structural preview renderer
    highlight_lang: str = ""  # code-fence language; defaults to id

    @property
    def fence_lang(self) -> str:
        """Language tag for a ```lang code fence (defaults to the kind id)."""
        return self.highlight_lang or self.id


CATEGORIES: tuple[Category, ...] = (
    Category("documents", "Documents", 0),
    Category("notes", "Notes & text", 1),
    Category("presentations", "Presentations", 2),
    Category("data", "Data & config", 3),
    Category("code", "Code", 4),
    Category("ebooks", "E-books", 5),
    Category("web", "Web", 6),
    Category("notebooks", "Notebooks", 7),
)


def _code(id: str, label: str, suffixes: tuple[str, ...], highlight_lang: str = "") -> KindSpec:
    """A source-code kind: parsed by extract/code.py, rendered as a fence."""
    return KindSpec(id, label, suffixes, "code", "code", True, highlight_lang)


KIND_SPECS: tuple[KindSpec, ...] = (
    # Documents
    KindSpec("pdf", "PDF", (".pdf",), "pdf", "documents", True),
    KindSpec("docx", "Word", (".docx",), "docx", "documents", True),
    KindSpec("odt", "OpenDocument Text", (".odt",), "odf", "documents", True),
    # Notes & text
    KindSpec("md", "Markdown", (".md", ".markdown"), "markdown", "notes", True),
    KindSpec("txt", "Plain text", (".txt",), "plain", "notes", False),
    # Presentations
    KindSpec("pptx", "PowerPoint", (".pptx",), "pptx", "presentations", True),
    KindSpec("odp", "OpenDocument Presentation", (".odp",), "odf", "presentations", True),
    # Data & config
    KindSpec("json", "JSON", (".json",), "data", "data", True),
    KindSpec("yaml", "YAML", (".yaml", ".yml"), "data", "data", True),
    KindSpec("toml", "TOML", (".toml",), "data", "data", True),
    KindSpec("xml", "XML", (".xml",), "data", "data", True),
    KindSpec("ini", "INI", (".ini",), "data", "data", True),
    KindSpec("csv", "CSV", (".csv",), "data", "data", True),
    KindSpec("tsv", "TSV", (".tsv",), "data", "data", True),
    KindSpec("ods", "OpenDocument Spreadsheet", (".ods",), "odf", "data", True),
    # Code (curated; extend by adding a row)
    _code("python", "Python", (".py", ".pyi", ".pyw")),
    _code("javascript", "JavaScript", (".js", ".mjs", ".cjs")),
    _code("typescript", "TypeScript", (".ts", ".mts", ".cts")),
    _code("tsx", "TSX", (".tsx",), "typescript"),
    _code("jsx", "JSX", (".jsx",), "javascript"),
    _code("c", "C", (".c", ".h")),
    _code("cpp", "C++", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")),
    _code("csharp", "C#", (".cs",)),
    _code("go", "Go", (".go",)),
    _code("rust", "Rust", (".rs",)),
    _code("java", "Java", (".java",)),
    _code("kotlin", "Kotlin", (".kt", ".kts")),
    _code("swift", "Swift", (".swift",)),
    _code("ruby", "Ruby", (".rb",)),
    _code("php", "PHP", (".php",)),
    _code("scala", "Scala", (".scala", ".sc")),
    _code("shell", "Shell", (".sh", ".bash", ".zsh"), "bash"),
    _code("sql", "SQL", (".sql",)),
    _code("r", "R", (".r",)),
    _code("lua", "Lua", (".lua",)),
    _code("perl", "Perl", (".pl", ".pm")),
    _code("dart", "Dart", (".dart",)),
    # E-books
    KindSpec("epub", "EPUB", (".epub",), "epub", "ebooks", True),
    # Web
    KindSpec("html", "HTML", (".html", ".htm"), "web", "web", True),
    # Notebooks
    KindSpec("ipynb", "Jupyter Notebook", (".ipynb",), "notebook", "notebooks", True),
)


# ── Derived lookups (import these; do not re-derive elsewhere) ──────────────
KIND_BY_ID: dict[str, KindSpec] = {k.id: k for k in KIND_SPECS}
CATEGORY_BY_ID: dict[str, Category] = {c.id: c for c in CATEGORIES}
ALL_KIND_IDS: tuple[str, ...] = tuple(k.id for k in KIND_SPECS)
CATEGORY_IDS: tuple[str, ...] = tuple(c.id for c in CATEGORIES)
SUFFIX_TO_MODULE: dict[str, str] = {
    sfx: k.extractor_module for k in KIND_SPECS for sfx in k.suffixes
}
SUFFIX_TO_KIND: dict[str, str] = {sfx: k.id for k in KIND_SPECS for sfx in k.suffixes}
MARKDOWN_RENDERED_KINDS: frozenset[str] = frozenset(k.id for k in KIND_SPECS if k.markdown_rendered)


def _kinds_in_category() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {c.id: [] for c in CATEGORIES}
    for k in KIND_SPECS:
        out[k.category].append(k.id)
    return {cid: tuple(ids) for cid, ids in out.items()}


# category id → its member kind ids, in KIND_SPECS order
KINDS_IN_CATEGORY: dict[str, tuple[str, ...]] = _kinds_in_category()


def supported_suffixes() -> frozenset[str]:
    """Every suffix fnd can index (drives the file walker's gate)."""
    return frozenset(SUFFIX_TO_MODULE)


def kind_for_suffix(suffix: str) -> str | None:
    """Fine-grained kind id for a file suffix (case-insensitive), or None."""
    return SUFFIX_TO_KIND.get(suffix.lower())


def module_for_suffix(suffix: str) -> str | None:
    """Extractor module name for a file suffix (case-insensitive), or None."""
    return SUFFIX_TO_MODULE.get(suffix.lower())


def _validate() -> None:
    """Fail fast on a mis-authored registry (duplicate suffix, bad category)."""
    if len(KIND_BY_ID) != len(KIND_SPECS):
        raise ValueError("duplicate kind id in KIND_SPECS")
    seen: dict[str, str] = {}
    for k in KIND_SPECS:
        if k.category not in CATEGORY_BY_ID:
            raise ValueError(f"kind {k.id!r} has unknown category {k.category!r}")
        for sfx in k.suffixes:
            if sfx != sfx.lower() or not sfx.startswith("."):
                raise ValueError(f"suffix {sfx!r} for {k.id!r} must be lowercase and dot-prefixed")
            if sfx in seen:
                raise ValueError(f"suffix {sfx!r} claimed by both {seen[sfx]!r} and {k.id!r}")
            seen[sfx] = k.id


_validate()


def split_type_globs(globs: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(kind ids, the globs that are not a plain file-type glob)``.

    A kind is selected iff any of its ``**/*<suffix>`` globs is present; those
    are consumed. ``includes = ["**/*.md"]`` and ``filters.kinds = ["md"]`` are
    the same statement, and this is what lets one be written as the other.
    """
    remaining = list(globs)
    kinds: list[str] = []
    for spec in KIND_SPECS:
        kglobs = [f"**/*{sfx}" for sfx in spec.suffixes]
        if any(g in remaining for g in kglobs):
            kinds.append(spec.id)
            remaining = [g for g in remaining if g not in kglobs]
    return kinds, remaining
