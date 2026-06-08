"""Render ``mermaid`` fences as terminal text-art via termaid.

Isolated so the fence widget only sees ``render() -> Text | None``;
``None`` means "fall back to source". termaid returns an empty Text on
non-mermaid input rather than raising, so empty output is a fallback too.
"""

from __future__ import annotations

from functools import lru_cache

from rich.text import Text

try:
    import termaid
except Exception:  # pragma: no cover - optional dep absent
    termaid = None  # type: ignore[assignment]

_AVAILABLE = termaid is not None

# Above this the A* layout isn't worth the on-loop cost — fall back to source.
MAX_SOURCE_LINES = 200


class MermaidRenderer:
    """Render mermaid source to a Rich ``Text`` diagram, or ``None``."""

    def render(self, source: str, theme: str = "default", use_ascii: bool = False) -> Text | None:
        if not _AVAILABLE or not source.strip():
            return None
        if source.count("\n") + 1 > MAX_SOURCE_LINES:
            return None
        return _render_cached(source, theme, use_ascii)


@lru_cache(maxsize=128)
def _render_cached(source: str, theme: str, use_ascii: bool) -> Text | None:
    if termaid is None:  # pragma: no cover - guarded by _AVAILABLE
        return None
    try:
        out = termaid.render_rich(source, theme=theme, use_ascii=use_ascii)
    except Exception:
        return None
    if not out.plain.strip():
        return None
    return out
