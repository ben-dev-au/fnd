"""RunnerResult dataclass and metric helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

# Header / list / table counters operate on the Markdown a runner returns.
# Approximate by construction — we measure structure *recovered*, not
# structure *present in source*.
_H_RE = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)
_LIST_RE = re.compile(r"^\s{0,3}([-*+]|\d+\.)\s+\S", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
_WS_RE = re.compile(r"\s+")


@dataclass
class RunnerResult:
    wall_ms: float
    rss_delta_mb: float
    output_md: str
    n_h1: int = 0
    n_h2: int = 0
    n_h3: int = 0
    n_h4: int = 0
    n_h5: int = 0
    n_h6: int = 0
    n_tables: int = 0
    n_list_items: int = 0
    token_jaccard: float = 0.0
    reading_order_hash: str = ""
    sample_300: str = ""
    crashed: bool = False
    error: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def to_row(self) -> dict[str, object]:
        row = asdict(self)
        # output_md is written to a sibling file, not the CSV.
        row.pop("output_md", None)
        # Flatten extra into top-level keys with extra_ prefix.
        extra = row.pop("extra", {}) or {}
        for k, v in extra.items():
            row[f"extra_{k}"] = v
        return row


def count_headers(md: str) -> tuple[int, int, int, int, int, int]:
    counts = [0, 0, 0, 0, 0, 0]
    for m in _H_RE.finditer(md):
        depth = len(m.group(1))
        counts[depth - 1] += 1
    return tuple(counts)  # type: ignore[return-value]


def count_list_items(md: str) -> int:
    return sum(1 for _ in _LIST_RE.finditer(md))


def count_tables(md: str) -> int:
    """Count *contiguous* Markdown table blocks.

    A table is one or more `| ... |` rows with no blank line between them.
    """
    in_table = False
    n = 0
    for line in md.splitlines():
        is_row = bool(_TABLE_ROW_RE.match(line))
        if is_row and not in_table:
            n += 1
            in_table = True
        elif not is_row:
            in_table = False
    return n


def _tokens(s: str) -> set[str]:
    return set(_WS_RE.sub(" ", s.lower()).strip().split(" "))


def token_jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    union = ta | tb
    if not union:
        return 1.0
    return len(ta & tb) / len(union)


def reading_order_hash(md: str) -> str:
    norm = _WS_RE.sub(" ", md.lower()).strip()
    return hashlib.sha1(norm.encode("utf-8"), usedforsecurity=False).hexdigest()


def populate_structural_metrics(result: RunnerResult, *, baseline_md: str) -> None:
    md = result.output_md
    h1, h2, h3, h4, h5, h6 = count_headers(md)
    result.n_h1, result.n_h2, result.n_h3 = h1, h2, h3
    result.n_h4, result.n_h5, result.n_h6 = h4, h5, h6
    result.n_tables = count_tables(md)
    result.n_list_items = count_list_items(md)
    result.token_jaccard = token_jaccard(md, baseline_md)
    result.reading_order_hash = reading_order_hash(md)
    result.sample_300 = md[:300]


CSV_COLUMNS: tuple[str, ...] = (
    "pdf",
    "page",
    "runner",
    "wall_ms",
    "rss_delta_mb",
    "n_h1",
    "n_h2",
    "n_h3",
    "n_h4",
    "n_h5",
    "n_h6",
    "n_tables",
    "n_list_items",
    "token_jaccard",
    "reading_order_hash",
    "sample_300",
    "crashed",
    "error",
    "output_md_path",
)
