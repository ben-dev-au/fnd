"""Row-label and score formatting for the results tree."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len

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
    "_styled_state_row",
    "_trim_redundant_heading",
    "disambiguated_names",
    "reapply_state_marker",
    "state_colour",
]

_PASS_GLYPHS = {0: "●", 1: "~", 2: "⊕", 3: "❝"}
# The engine matched this chunk, but nothing in the text the preview renders
# carries a paintable span — selecting the row lands somewhere with no visible
# highlight. The row is still listed (the engine's match is what makes a result
# a result); the glyph says the match is there but couldn't be located, so a
# highlighting regression shows up as marks on screen instead of results
# quietly disappearing. See :mod:`fnd.tui.match_evidence`.
_UNLOCATABLE_GLYPH = "◌"

# The index's copy no longer matches the disk. One glyph for gone and for
# edited, because the user's move is the same either way: reindex.
_STALE_GLYPH = "⚠"

_MARKER_STYLES: dict[str, Any] = {}


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


#: Which theme colour a tri-state marker carries. Only the two states that
#: change what is indexed get a hue; ``○`` stays neutral so it does not
#: compete with them, and ``◐`` is a roll-up rather than a state of its own.
STATE_COLOUR_VARIABLE = {"●": "success", "⊘": "error"}

#: Used when the theme names no such variable, which a theme may omit: a
#: colour dropped in silence is indistinguishable from the feature being absent.
STATE_COLOUR_FALLBACK = {"●": "green", "⊘": "red"}


def state_colour(marker: str, variables: Mapping[str, str]) -> str:
    """The colour a state marker carries, given a theme's variables.

    Stripped first: callers pad a marker to align a leaf under its branch,
    and a padded marker that silently lost its colour is the exact bug this
    is meant to prevent. Falls back where the theme names no such colour.
    """
    variable = STATE_COLOUR_VARIABLE.get(marker.strip())
    if not variable:
        return ""
    return variables.get(variable, "") or STATE_COLOUR_FALLBACK.get(marker, "")


def _styled_state_row(marker: str, rest: str, colour: str) -> Any:
    """A tri-state row whose marker carries its meaning as colour too.

    ``⊘`` and ``○`` differ by a hairline and mean opposites, so shape alone
    was doing all the work. The span covers the glyph only, and it is a span
    rather than a base style because a tree paints rows with its own style and
    a base one loses to it.
    """
    from rich.text import Text

    text = Text(f"{marker}{rest}")
    if colour:
        text.stylize(_marker_style(colour), 0, len(marker))
    return text


def _marker_style(colour: str) -> Any:
    """Cached so a rendered span can be recognised as a marker's by identity."""
    from rich.style import Style

    style = _MARKER_STYLES.get(colour)
    if style is None:
        style = Style(color=colour)
        _MARKER_STYLES[colour] = style
    return style


def reapply_state_marker(rendered: Any, label: Any) -> Any:
    """Put a marker's colour back over a row style applied on top of it.

    Textual stylises a whole label with the cursor's component style, and a
    span added last wins, so the marker would lose its colour on exactly the
    row the user is looking at. Only styles this module minted are restored.
    """
    from rich.text import Text

    if not isinstance(label, Text) or not isinstance(rendered, Text):
        return rendered
    offset = len(rendered.plain) - len(label.plain)
    if offset < 0:
        return rendered
    known = set(_MARKER_STYLES.values())
    for span in label.spans:
        if span.style in known:
            rendered.stylize(span.style, span.start + offset, span.end + offset)
    return rendered


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


def _format_hit_label(
    h: Hit, *, max_score: float = 0.0, match_visible: bool = True, body_budget: int = 0
) -> Any:
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
    if body_budget > 0 and cell_len(body) > body_budget:
        # The locator always survives and the snippet, never dropped whole,
        # fills the space after it. Only a locator that overruns falls back to
        # its distinguishing tail (24 sibling `section` rows).
        room = body_budget - cell_len(loc) - 2
        if snippet and room >= 4:
            body = f"{loc}  {_shorten(snippet, room)}"
        else:
            body = _elide_middle_keep_suffix(loc, body_budget)
    glyph = _PASS_GLYPHS.get(h.pass_index, "")
    pass_marker = f" {glyph}" if h.pass_index > 0 else ""
    # Leading, not trailing: locator + 80-char snippet routinely overruns the
    # results pane, so the row is hard-truncated at the border and anything
    # appended to the end is never drawn. Verified in a real terminal — the
    # marker was being emitted correctly and clipped off screen every time.
    prefix = "" if match_visible else f"{_UNLOCATABLE_GLYPH} "
    return _build_label(f"{prefix}{body}{pass_marker}", h.score, max_score)


def disambiguated_names(paths: Sequence[str]) -> dict[str, str]:
    """path → the shortest tail that tells it apart from the others shown.

    Rows carry the basename, so a build folder's `out-01.md` and a note of the
    same name were two identical rows: a user asking "which ones?" could not
    tell from the result which file it was.
    """
    from collections import defaultdict

    # Split each path once and group the rivals once. Recomputing `Path(q).parts`
    # inside the depth loop, over every other path, measured 73 ms at 200 rows,
    # and `_refresh_status` reaches this from twenty call sites.
    parts_by: dict[str, tuple[str, ...]] = {p: Path(p).parts for p in paths}
    by_name: dict[str, list[str]] = defaultdict(list)
    for path, parts in parts_by.items():
        by_name[parts[-1] if parts else path].append(path)

    def _shared_tail(a: tuple[str, ...], b: tuple[str, ...]) -> int:
        n = 0
        while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
            n += 1
        return n

    out: dict[str, str] = {}
    for name, group in by_name.items():
        if len(group) == 1:
            out[group[0]] = name
            continue
        # Sorted on the reversed path, the rival sharing the deepest tail with
        # a row is one of its two neighbours, so each row compares twice
        # instead of against every other row.
        order = sorted(group, key=lambda q: tuple(reversed(parts_by[q])))
        for i, path in enumerate(order):
            parts = parts_by[path]
            deepest = max(
                (
                    _shared_tail(parts, parts_by[order[j]])
                    for j in (i - 1, i + 1)
                    if 0 <= j < len(order)
                ),
                default=0,
            )
            depth = min(max(2, deepest + 1), len(parts))
            out[path] = "/".join(parts[-depth:])
    return out


def is_stale(g: FileGroup) -> bool:
    """Whether the index's copy of this file no longer matches the disk.

    Three answers, not two. Gone and edited both mean the preview would show
    something the file does not say. A file we cannot STAT is unknown, and
    marking unknown as changed is the same conflation that let an unreadable
    source read as an empty one.

    ``!=``, not ``>``: a file restored from a backup carries an older mtime and
    is just as stale. Matches `index_runner._should_reprocess`, which is what
    decides whether the indexer would re-read it.
    """
    indexed = max((h.mtime for h in g.hits), default=0)
    try:
        on_disk = int(Path(g.path).stat().st_mtime)
    except FileNotFoundError:
        return True
    except OSError:
        # Cannot look. Saying nothing beats saying the wrong thing.
        return False
    return indexed > 0 and on_disk != indexed


def _format_file_label(
    g: FileGroup,
    *,
    max_score: float = 0.0,
    name_budget: int = 0,
    display_name: str = "",
    stale: bool = False,
) -> Any:
    name = display_name or Path(g.path).name
    # Charged BEFORE eliding: added afterwards it pushed the row 2 cells past
    # its budget, and the cells it took were the suffix the elision keeps.
    marker = f"{_STALE_GLYPH} " if stale else ""
    if name_budget > 0:
        name = _elide_middle_keep_suffix(name, max(1, name_budget - cell_len(marker)))
    return _build_label(f"{marker}{name}", g.top_score, max_score)
