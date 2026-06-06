"""LigatureRepairer + LigatureRepairTier.

The layout text engine emits U+FFFD for ligature glyphs (ff/fi/fl/ffi/ffl)
in fonts whose ToUnicode CMap lacks entries for them. The flat text layer
(MuPDF's standard engine) resolves the same glyphs, so it is used as
ground truth to rebuild only the words a ligature broke — standalone
U+FFFD (non-ligature unmapped glyphs) is left untouched.
"""

from __future__ import annotations

from fnd.extract.recovery import (
    FLAG_LIGATURE_REPAIRED,
    ExtractionContext,
    LigatureRepairer,
    LigatureRepairTier,
    PageExtraction,
)

FFFD = "�"


def _ctx(flat: str) -> ExtractionContext:
    return ExtractionContext(doc=object(), page=object(), page_index=0, path="x.pdf", flat=flat)


# ── LigatureRepairer ─────────────────────────────────────────────────────────
def test_repairs_inword_ligatures_from_flat_vocab() -> None:
    r = LigatureRepairer()
    md = f"monitoring of IPsec tra{FFFD}c and mail {FFFD}ows"
    flat = "monitoring of IPsec traffic and mail flows"
    assert r.repair(md, flat) == "monitoring of IPsec traffic and mail flows"


def test_repair_preserves_leading_capital() -> None:
    r = LigatureRepairer()
    assert r.repair(f"Tra{FFFD}c report", "the traffic report") == "Traffic report"


def test_repair_disambiguates_via_flat_vocab() -> None:
    """``fi`` and ``fl`` both fit ``{FFFD}nancial`` slot-wise; only the
    vocab-matching expansion is chosen."""
    r = LigatureRepairer()
    assert r.repair(f"a {FFFD}nancial firm", "a financial firm") == "a financial firm"


def test_noop_when_no_replacement_char() -> None:
    r = LigatureRepairer()
    md = "clean text with no artifacts"
    assert r.repair(md, "clean text with no artifacts") is md  # identity fast-path


def test_leaves_standalone_replacement_char_untouched() -> None:
    """A bare U+FFFD (a non-ligature unmapped glyph) has no word skeleton
    to match — leave it rather than inventing 'ff'/'fi'."""
    r = LigatureRepairer()
    md = f"see {FFFD} here"
    assert r.repair(md, "see X here") == md


def test_bare_replacement_not_rewritten_even_when_vocab_has_ligature() -> None:
    """A standalone U+FFFD must stay put even when the flat layer contains
    a bare 'ff'/'fi'/'fl' token — repairing only fires inside a word."""
    r = LigatureRepairer()
    md = f"tempo {FFFD} marking ff dynamics"
    assert r.repair(md, "tempo X marking ff dynamics") == md


def test_repair_preserves_capital_on_midword_ligature() -> None:
    """Capitalised words with a mid-word ligature keep the capital — the
    real corpus case ('Miscon<?>gured', 'Con<?>dentiality')."""
    r = LigatureRepairer()
    assert r.repair(f"Miscon{FFFD}gured CAs", "a misconfigured ca") == "Misconfigured CAs"


def test_leaves_unmatched_word_untouched() -> None:
    """No ligature expansion of the broken word is in the flat vocab."""
    r = LigatureRepairer()
    md = f"the {FFFD}zzqq token"
    assert r.repair(md, "the zzqq token") == md


def test_caps_pathological_multifffd_token() -> None:
    """A token with many U+FFFD slots would explode the candidate
    product — leave it untouched past the cap rather than churn."""
    r = LigatureRepairer()
    md = "a" + FFFD * 8 + "b"
    assert r.repair(md, "anything here") == md


# ── LigatureRepairTier ───────────────────────────────────────────────────────
def test_tier_repairs_and_stamps_provenance() -> None:
    tier = LigatureRepairTier(LigatureRepairer())
    out = tier.refine(
        _ctx("intrusion detection traffic"),
        PageExtraction(markdown=f"intrusion detection tra{FFFD}c", tier="production-layout"),
    )
    assert out.markdown == "intrusion detection traffic"
    assert out.tier == "ligature-repair"
    assert FLAG_LIGATURE_REPAIRED in out.flags


def test_tier_passthrough_when_no_replacement_char() -> None:
    tier = LigatureRepairTier(LigatureRepairer())
    current = PageExtraction(markdown="already clean", tier="production-layout")
    out = tier.refine(_ctx("already clean"), current)
    assert out is current  # untouched, provenance preserved


def test_tier_passthrough_when_markdown_empty() -> None:
    tier = LigatureRepairTier(LigatureRepairer())
    current = PageExtraction(markdown="", tier="none")
    assert tier.refine(_ctx("flat words"), current) is current


def test_tier_keeps_current_when_nothing_repairable() -> None:
    """U+FFFD present but unrepairable (standalone) — don't restamp the
    tier or set the flag for a no-op."""
    tier = LigatureRepairTier(LigatureRepairer())
    current = PageExtraction(markdown=f"a {FFFD} b", tier="production-layout")
    out = tier.refine(_ctx("a X b"), current)
    assert out is current


def test_tier_preserves_prior_flags_on_repair() -> None:
    tier = LigatureRepairTier(LigatureRepairer())
    current = PageExtraction(
        markdown=f"tra{FFFD}c", tier="production-layout", flags=frozenset({"texture-recovered"})
    )
    out = tier.refine(_ctx("traffic"), current)
    assert "texture-recovered" in out.flags
    assert FLAG_LIGATURE_REPAIRED in out.flags


# ── Pipeline wiring ──────────────────────────────────────────────────────────
def test_recovery_pipeline_runs_repair_after_layout() -> None:
    """The repair tier must see the layout baseline before the quality
    gates assess coverage, so it sits immediately after the layout tier."""
    from fnd.extract import pdf

    names = [type(t).__name__ for t in pdf._recovery_pipeline()._tiers]
    assert names.index("LigatureRepairTier") == names.index("ProductionLayoutTier") + 1
