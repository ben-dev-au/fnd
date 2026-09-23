"""A scan that stopped early narrows nothing.

sample_source stops at 4000 files in walk order, so a picker offering only what
it reached turns 4500 notes beside 200 PDFs into Markdown alone, and "md and
pdf" cannot be expressed on that screen at all. Walk order is reverse
alphabetical, so the 4000 are systematically the last 4000: a year-foldered
corpus loses its earliest years from every picker.

`truncated` does not reach `_kind_items`, so each pair of cases here exercises
one path; the assertions still fail the moment the picker goes back to offering
what it saw. The flag earns its keep elsewhere, and the last test holds it to
that so removing it cannot pass silently.
"""

from __future__ import annotations

from fnd.filters import FilterSpec
from fnd.filters.scan import SourceSample
from fnd.filters.tree_model import spec_branches
from fnd.kinds import ALL_KIND_IDS


def _offered(sample: SourceSample, spec: FilterSpec | None = None) -> set[str]:
    branches = spec_branches(spec or FilterSpec(), sample)
    kinds = next((b for b in branches if b.id == "kinds"), None)
    assert kinds is not None
    return {i.removeprefix("kind:") for g in kinds.groups for i, _label in g.items}


def test_a_truncated_scan_offers_every_kind() -> None:
    seen_only_md = SourceSample(kinds={"md": 4000}, tags={}, truncated=True)
    assert _offered(seen_only_md) == set(ALL_KIND_IDS)


def test_a_complete_scan_offers_every_kind_too() -> None:
    """A picker showing only today's types has to be revisited as the corpus
    grows; a filter set once should keep holding."""
    whole_source = SourceSample(kinds={"md": 3}, tags={}, truncated=False)
    assert _offered(whole_source) == set(ALL_KIND_IDS)


def test_a_configured_kind_survives_either_way() -> None:
    spec = FilterSpec(kinds=("pdf",))
    for truncated in (True, False):
        sample = SourceSample(kinds={"md": 3}, tags={}, truncated=truncated)
        assert "pdf" in _offered(sample, spec), truncated


def test_a_truncated_branch_may_claim_every_type() -> None:
    """With every kind offered, ticking them all really is "every type"."""
    sample = SourceSample(kinds={"md": 4000}, tags={}, truncated=True)
    kinds = next(b for b in spec_branches(FilterSpec(), sample) if b.id == "kinds")
    assert kinds.full_label == "every type"


def test_the_counts_still_say_what_is_there_now() -> None:
    """Offering every kind must not cost the counts: the rows say what can be
    chosen, the counts say what the source holds today."""
    sample = SourceSample(kinds={"md": 40, "pdf": 3}, tags={}, truncated=False)
    kinds = next(b for b in spec_branches(FilterSpec(), sample) if b.id == "kinds")
    labels = {i: label for g in kinds.groups for i, label in g.items}
    assert "40" in labels["kind:md"], labels["kind:md"]
    assert "3" in labels["kind:pdf"], labels["kind:pdf"]
    assert "·" not in labels["kind:epub"], "a kind with none seen carries no count"


def test_the_flag_still_reaches_the_user_somewhere() -> None:
    """`truncated` does not narrow the picker, so the only thing it does is
    say so, and nothing above would fail if that went too."""
    import inspect

    from fnd.tui import settings_screen

    source = inspect.getsource(settings_screen)
    assert "truncated" in source, "the flag stopped reaching the screen entirely"
    assert "partial scan" in source, "the scan stopped saying it had been cut short"
