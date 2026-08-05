"""Row-label and score formatting for the results tree."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fnd.display_text import sanitise_display_text

if TYPE_CHECKING:
    from fnd.query import FileGroup, Hit

__all__ = [
    "_build_label",
    "_elide_middle_keep_suffix",
    "_format_file_label",
    "_format_hit_label",
    "_score_bar",
    "_score_style",
    "_shorten",
    "_styled_action_label",
    "_styled_parent_label",
    "_trim_redundant_heading",
]

_PASS_GLYPHS = {0: "●", 1: "~", 2: "⊕", 3: "❝"}
# The engine matched this chunk, but nothing in the text the preview renders
# carries a paintable span — selecting the row lands somewhere with no visible
# highlight. The row is still listed (the engine's match is what makes a result
# a result); the glyph says the match is there but couldn't be located, so a
# highlighting regression shows up as marks on screen instead of results
# quietly disappearing. See :mod:`fnd.tui.match_evidence`.
_UNLOCATABLE_GLYPH = "◌"


def _score_bar(  # pyright: ignore[reportUnusedFunction]
    *,
    score: float,
    max_score: float,
    width: int = 5,
) -> str:
    """Pure utility kept for the legacy test surface.

    The TUI no longer draws score bars — the user's feedback on the
    eighth-block and full-block variants was that they read as visual
    noise. The current label formatters use :func:`_score_style`
    instead, colouring the numeric score in line with the theme.
    """
    if max_score <= 0:
        return " " * width
    ratio = max(0.0, min(1.0, score / max_score))
    full = round(ratio * width)
    return "█" * full + " " * (width - full)


def _score_style(score: float, max_score: float) -> str:
    """Rich-style spec for a numeric score, graded by relative position.

    Walks the tokyo-night accent palette from a vivid green (top tier)
    through cyan and accent-blue down to a muted slate. The score is
    the only place we lean on colour for ranking signal, so the steps
    are saturated enough to read at a glance without becoming a
    stoplight.
    """
    if max_score <= 0:
        return "dim"
    ratio = max(0.0, min(1.0, score / max_score))
    if ratio >= 0.85:
        return "bold #9ece6a"  # tokyo-night green — leader
    if ratio >= 0.6:
        return "#7dcfff"  # cyan
    if ratio >= 0.35:
        return "#7aa2f7"  # accent blue (theme default)
    if ratio >= 0.15:
        return "#bb9af7"  # cool magenta — fades from accent
    return "dim #565f89"


def _styled_parent_label(label: Any) -> Any:
    """Render a tree-parent label in the muted "structural row" style.

    Parents in the Results and Filters trees aren't cursor-selectable
    when expanded (`_skip_expanded_parents`); Collections parents stay
    selectable but get the same visual treatment so the parent/child
    distinction reads consistently across all three trees.
    """
    from rich.text import Text

    if isinstance(label, Text):
        styled = label.copy()
        styled.stylize("dim")
        return styled
    return Text(str(label), style="dim")


def _styled_action_label(label: Any, colour: str) -> Any:
    """Render a control row (Clear filters, Match mode) in ``colour``.

    Actions sit between the dim category headers and a live selection: they
    take the inactive-pane colour ($primary) so they read as interactive
    without competing with the focused-pane accent. Falls back to plain text
    if the theme colour is missing.
    """
    from rich.text import Text

    style = colour or ""
    if isinstance(label, Text):
        styled = label.copy()
        if style:
            styled.stylize(style)
        return styled
    return Text(str(label), style=style)


def _build_label(text: str, score: float, max_score: float) -> Any:
    """Tree label combining a coloured numeric score (left, fixed width)
    with the file/section text (right, may truncate cleanly).

    Score-first layout means the colour-coded ranking signal is always
    visible regardless of filename length — long titles truncate
    against the right edge of the pane without ever eating the score.
    """
    from rich.text import Text

    # Last-line-of-defence sanitise so a raw ``\t`` (or other control char) from
    # any source — snippet, heading crumb, filename — can never over-run the row
    # and corrupt the pane border. Snippets are already cleaned at their source
    # (fnd.query._make_snippet); this covers the locator/filename paths too.
    text = sanitise_display_text(text)
    label = Text()
    if max_score > 0 and score > 0:
        label.append(f"{score:5.2f}", style=_score_style(score, max_score))
        label.append("  ")
    else:
        label.append(" " * 7)
    label.append(text)
    return label


_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def _trim_redundant_heading(heading_path: str, title: str, path: str) -> str:
    """Strip leading segments that just repeat words from the filename or
    title. The result tree's parent row already shows the filename, so
    prefixing every section row with the same words is just clutter.

    A leading ``Templates`` segment is dropped when ``Templates`` also
    appears as a word in the file basename (``DPC Wk8 Notes - Templates,
    Strategy Pattern & C++ Streams``) or in the title — covers both the
    pure ``# Templates`` H1 case and the deep multi-word filename case.
    """
    if not heading_path:
        return ""

    def _words(s: str) -> set[str]:
        return {w for w in _NON_WORD_RE.split(s.lower()) if w}

    parts = [p.strip() for p in heading_path.split(">") if p.strip()]
    haystack = _words(Path(path).stem) | _words(title or "")
    # If every segment is just a word from the filename / title, the
    # whole crumb is redundant — keep the deepest one as the location
    # marker, or drop it entirely when there's only one segment so the
    # caller can fall back to a chunk locator.
    if parts and all(_words(p).issubset(haystack) for p in parts):
        return parts[-1] if len(parts) > 1 else ""
    while parts and _words(parts[0]).issubset(haystack):
        parts.pop(0)
    return " > ".join(parts)


def _shorten(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an ellipsis suffix."""
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _elide_middle_keep_suffix(name: str, max_width: int) -> str:
    """Middle-truncate ``name`` to ``max_width`` chars, keeping the extension
    visible: ``really_long_report_final_v3.pdf`` -> ``really_…nal_v3.pdf``.

    A terminal's default right-clip drops the extension — the one part that
    says what kind of file it is — so we elide the stem's middle and keep both
    ends plus the suffix. When even one stem char won't fit we still show
    ``…<suffix>``; only when the suffix itself can't fit (``max_width`` shorter
    than ``…`` + suffix) do we fall back to a plain right-truncation that drops
    it. Char-counted (like ``_shorten``); wide glyphs aside.
    """
    if len(name) <= max_width:
        return name
    if max_width <= 1:
        return name[: max(0, max_width)]
    suffix = Path(name).suffix
    stem = name[: len(name) - len(suffix)] if suffix else name
    stem_budget = max_width - len(suffix) - 1  # 1 cell for the ellipsis
    if stem_budget < 0:
        # Even "…" + suffix won't fit; show leading chars, plain-truncated.
        return name[: max_width - 1] + "…"
    head = (stem_budget + 1) // 2
    tail = stem_budget - head
    return stem[:head] + "…" + (stem[-tail:] if tail else "") + suffix


def _format_hit_label(h: Hit, *, max_score: float = 0.0, match_visible: bool = True) -> Any:
    """Result-tree row label: short locator left, snippet right.

    Locator is a few chars (page / slide / trimmed heading / chunk N)
    so the body snippet — the actually useful context for "is this
    the match I want" — claims most of the row width.

    ``match_visible=False`` prepends :data:`_UNLOCATABLE_GLYPH`: the row stays,
    but the user is told the preview won't be able to show them the match.
    """
    if h.page_label:
        loc = f"p.{h.page_label}"
    elif h.page:
        loc = f"p.{h.page}"
    elif h.slide:
        loc = f"s.{h.slide}"
    else:
        trimmed = _trim_redundant_heading(h.heading_path, h.title, h.path)
        loc = _shorten(trimmed, 18) if trimmed else f"§{h.chunk_seq + 1}"
    snippet = _shorten(h.snippet, 80) if h.snippet else ""
    body = f"{loc}  {snippet}" if snippet else loc
    glyph = _PASS_GLYPHS.get(h.pass_index, "")
    pass_marker = f" {glyph}" if h.pass_index > 0 else ""
    # Leading, not trailing: locator + 80-char snippet routinely overruns the
    # results pane, so the row is hard-truncated at the border and anything
    # appended to the end is never drawn. Verified in a real terminal — the
    # marker was being emitted correctly and clipped off screen every time.
    prefix = "" if match_visible else f"{_UNLOCATABLE_GLYPH} "
    return _build_label(f"{prefix}{body}{pass_marker}", h.score, max_score)


def _format_file_label(g: FileGroup, *, max_score: float = 0.0, name_budget: int = 0) -> Any:
    name = Path(g.path).name
    if name_budget > 0:
        name = _elide_middle_keep_suffix(name, name_budget)
    return _build_label(name, g.top_score, max_score)
