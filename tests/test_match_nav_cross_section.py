"""Which section n/b hands over to once the current one is exhausted.

Document order, not results order: the results tree is ranked by score, so its
neighbouring row is somewhere else in the file. Wrapping at the file's ends
keeps a run of n returning to where it started.
"""

from __future__ import annotations

from types import SimpleNamespace

from textual.geometry import Offset, Region, Size

from fnd.matching import MatchSpec
from fnd.tui.match_navigator import MatchNavigator, adjacent_section

SEQS = [3, 7, 12]


def test_forward_takes_the_next_section_in_document_order() -> None:
    assert adjacent_section(SEQS, 7, forward=True) == 12


def test_back_takes_the_previous_section() -> None:
    assert adjacent_section(SEQS, 7, forward=False) == 3


def test_forward_wraps_to_the_files_first_section() -> None:
    assert adjacent_section(SEQS, 12, forward=True) == 3


def test_back_wraps_to_the_files_last_section() -> None:
    assert adjacent_section(SEQS, 3, forward=False) == 12


def test_a_lone_section_has_nowhere_to_hand_over_to() -> None:
    assert adjacent_section([7], 7, forward=True) is None
    assert adjacent_section([7], 7, forward=False) is None


def test_a_file_with_no_listed_sections_has_no_hop() -> None:
    assert adjacent_section([], 7, forward=True) is None


def test_an_unlisted_current_section_hands_over_either_side() -> None:
    """The focused chunk need not be listed itself; the hop is still ordered."""
    assert adjacent_section([3, 12], 7, forward=True) == 12
    assert adjacent_section([3, 12], 7, forward=False) == 3


def test_the_order_is_the_documents_not_the_arguments() -> None:
    assert adjacent_section([12, 3, 7], 3, forward=True) == 7


class _Hit:
    """As much of a results hit as the hand-over reads."""

    def __init__(self, parent_id: str, chunk_seq: int) -> None:
        self.parent_id = parent_id
        self.chunk_seq = chunk_seq


def _nav(
    sections: list[int],
    *,
    chunk_has_stop: bool,
    mounted: bool = True,
    count: int = 0,
    anchor_parent: str = "p",
    building: bool = False,
    flat: bool = False,
    highlights: bool = True,
) -> MatchNavigator:
    """A navigator on a plain chunk 7 of a file listing ``sections``."""
    chunk = SimpleNamespace(build_done=SimpleNamespace(is_set=lambda: not building))
    preview = SimpleNamespace(
        active=None if flat else SimpleNamespace(parent_doc_id="p"),
        chunk_widgets={7: chunk} if mounted else {},
        match_targets={7: SimpleNamespace(has_class=lambda _name: chunk_has_stop)},
    )
    app = SimpleNamespace(
        _preview=preview,
        _flat=SimpleNamespace(installed_key=("p", "sig") if flat else None),
        _preview_scroll=SimpleNamespace(
            anchor=SimpleNamespace(focus_chunk_seq=7, parent_id=anchor_parent),
            is_settling=False,
        ),
        call_after_refresh=lambda _cb, *_a, **_k: None,
        _diag_log=lambda _m: None,
        _refresh_preview_match_indicator=lambda: None,
        _refresh_footer_hints=lambda: None,
        _search=SimpleNamespace(
            groups=[SimpleNamespace(parent_id="p", hits=[_Hit("p", s) for s in sections])]
        ),
        _effective_match_spec=MatchSpec.from_query("alpha") if highlights else MatchSpec(),
    )
    nav = MatchNavigator(app)  # type: ignore[arg-type]
    nav._count = count
    return nav


def test_the_key_hint_holds_when_the_chunk_has_no_stop_but_the_file_does() -> None:
    """The hint gates on what n/b can do, and a hand-over is something."""
    assert _nav([3, 7], chunk_has_stop=False).current_chunk_has_stops()


def test_the_key_hint_drops_on_a_lone_section_with_no_reachable_match() -> None:
    assert not _nav([7], chunk_has_stop=False).current_chunk_has_stops()


def _nav_pressing(sections: list[int], *, chunk_has_stop: bool, stops: list[int], **kw: object):
    """Only the results-tree move is stubbed, so the hop's own guards run."""
    nav = _nav(sections, chunk_has_stop=chunk_has_stop, **kw)  # type: ignore[arg-type]
    pane = SimpleNamespace(
        scrollable_content_region=Region(0, 0, 80, 20),
        scroll_offset=Offset(0, 0),
        virtual_size=Size(80, 1000),
        max_scroll_y=980,
    )
    taken: list[bool] = []
    nav._pane = lambda: pane  # type: ignore[assignment]
    nav._region_stops = lambda _p: stops  # type: ignore[assignment]
    nav._current_chunk_extent = lambda _p: (0, 100)  # type: ignore[assignment]
    nav._select_section_row = lambda seq: (taken.append(seq), True)[1]  # type: ignore[assignment]
    return nav, taken


def test_a_press_before_the_layout_resolves_waits_rather_than_handing_over() -> None:
    """The chunk's stops are not measurable yet (mid-mount) but its match data is
    there — the press must wait for the layout, not leave the section."""
    nav, taken = _nav_pressing([3, 7], chunk_has_stop=True, stops=[])
    nav.next()
    assert taken == []


def test_a_section_whose_match_cannot_be_reached_hands_over() -> None:
    """No stops AND no match data: there is nothing here to walk to, so the
    press hands over rather than sitting inert."""
    nav, taken = _nav_pressing([3, 7], chunk_has_stop=False, stops=[])
    nav.next()
    assert taken == [3]  # the section after 7, wrapping to the file's first


def test_a_press_before_the_chunk_mounts_waits_rather_than_handing_over() -> None:
    """The window rebuild() zeroes the count for: no widget for the focus chunk
    and no cached count. Absence of evidence is not a dead end — wait."""
    nav, taken = _nav_pressing([3, 7], chunk_has_stop=False, stops=[], mounted=False, count=0)
    nav.next()
    assert taken == []


def test_a_press_during_a_cross_file_navigation_does_not_hand_over() -> None:
    """The anchor names the file being opened while ``active`` still names the one
    being left; resolving across that gap abandons the navigation."""
    nav, taken = _nav_pressing([3, 7], chunk_has_stop=False, stops=[], anchor_parent="other-file")
    nav.next()
    assert taken == []


def test_a_press_while_the_chunk_is_still_building_waits() -> None:
    """Blocks register during the build, so an empty match set there means "not
    yet" — the wide window, seconds on a big chunk."""
    nav, taken = _nav_pressing([3, 7], chunk_has_stop=False, stops=[], building=True)
    nav.next()
    assert taken == []


def test_a_flat_preview_does_not_hand_over() -> None:
    """A flat preview contributes no stops, so n/b are inert and never hand over."""
    nav, taken = _nav_pressing([3, 7], chunk_has_stop=False, stops=[], flat=True)
    nav.next()
    assert taken == []
    assert not nav.can_hop_section()


def test_highlights_off_leaves_the_keys_inert() -> None:
    """Highlights off empties the spec: no matches to walk, so no hand-over."""
    nav, taken = _nav_pressing([3, 7], chunk_has_stop=False, stops=[], highlights=False)
    nav.next()
    assert taken == []
    assert not nav.can_hop_section()
    assert not nav.current_chunk_has_stops()
