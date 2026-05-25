"""Per-PDF cost estimate for indexing.

Used by every confirm screen and disclosure that says "this will take
about N minutes". Two sources, in priority order:

1. The user's own recorded throughput. Every Update index run writes
   ``(signature, n_pdfs, cache_hits, cache_misses, elapsed_s,
   completed_at)`` to
   ``~/Library/Application Support/fnd/indexer_throughput.jsonl``;
   :func:`estimate_per_pdf_seconds` averages the last few runs whose
   ``signature`` matches the current extractor to get a calibrated
   figure for this machine and corpus.

2. A conservative fall-back when no same-signature runs have been
   recorded yet. Derived from the Phase-0 bake-off (``~0.2 s/page``
   hybrid throughput at ``~10 pages/PDF`` average). NEVER user-facing
   as a hard-coded constant beyond this module; consumers always call
   through ``estimate_per_pdf_seconds()`` so a calibrated figure
   replaces it as soon as the first real run completes.

Signature gating matters: structured (pymupdf4llm + docling)
extraction runs roughly ten times slower per PDF than flat-only. A
user who indexed before installing ``pdf-structure`` and again
afterwards has two cohorts of runs in their history; averaging
across both produces an ETA that is wrong in either direction. We
filter to runs whose signature equals the CURRENT
``fnd.extract.pdf.extractor_signature()``.

Un-tagged entries (predate signature tracking) are retagged in-place
with :func:`fnd.extract.pdf.legacy_flat_signature` so a user's
pre-pdf-structure baseline is preserved for future flat-only runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from platformdirs import user_data_dir

# Fall-back per-PDF cost (seconds) when no same-signature runs have
# been recorded yet. Conservative; users with fast machines see this
# drop after their first Update index run on the current signature.
FALLBACK_SECONDS_PER_PDF = 2.0

# How many recent runs to average. Older entries are ignored so a
# one-off slow run on a cold cache doesn't permanently inflate the
# estimate for someone whose typical run is much faster.
_SAMPLE_SIZE = 5

# Cap the persisted history at this many entries; older entries get
# rotated out. Keeps the file bounded; no need to track years of runs.
_MAX_HISTORY = 50


@dataclass(frozen=True)
class ThroughputRecord:
    """One Update index run's outcome, persisted for future ETAs."""

    completed_at: float  # unix timestamp
    n_pdfs: int
    cache_hits: int
    cache_misses: int
    elapsed_s: float
    signature: str  # extractor_signature() at run time


def _state_path() -> Path:
    """File where throughput records persist. Per-user, not per-venv,
    so the calibration follows you across virtualenv recreations."""
    return Path(user_data_dir("fnd")) / "indexer_throughput.jsonl"


def _current_signature() -> str:
    """Current extractor signature, lazy-imported so this module stays
    importable when the extractor's heavyweight deps are absent or
    failing (the calibrator must never block disclosures from rendering)."""
    try:
        from fnd.extract.pdf import extractor_signature

        return extractor_signature()
    except Exception:  # never fail callers over telemetry
        return "unknown"


def _legacy_signature() -> str:
    """Signature assigned to entries that pre-date signature tracking.

    Matches what a flat-only run on this machine would emit today, so
    a user who indexed before installing ``pdf-structure`` keeps their
    baseline for any future flat-only runs (e.g. after uninstalling)."""
    try:
        from fnd.extract.pdf import legacy_flat_signature

        return legacy_flat_signature()
    except Exception:
        return "flat|legacy"


def record_run(*, n_pdfs: int, cache_hits: int, cache_misses: int, elapsed_s: float) -> None:
    """Append a completed run to the persisted history. Trims to
    ``_MAX_HISTORY`` so the file stays bounded.

    Skips runs with fewer than 3 PDFs because tiny runs are dominated
    by setup cost and would skew the per-PDF average."""
    if n_pdfs < 3 or elapsed_s <= 0:
        return
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    rec = ThroughputRecord(
        completed_at=time.time(),
        n_pdfs=n_pdfs,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        elapsed_s=elapsed_s,
        signature=_current_signature(),
    )
    with contextlib.suppress(OSError):
        history = _load_history()
        history.append(rec)
        history = history[-_MAX_HISTORY:]
        # Write to a sibling temp file and os.replace into place so a
        # crash mid-write leaves the previous history file intact rather
        # than truncated. The throughput record is recoverable (next
        # successful run rebuilds the calibration), but a corrupt file
        # would mean every subsequent ETA falls back to the conservative
        # bake-off constant until the next clean run.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            for entry in history:
                fh.write(json.dumps(asdict(entry)) + "\n")
        os.replace(tmp_path, path)


def estimate_per_pdf_seconds(signature: str | None = None) -> float:
    """Calibrated per-PDF cost (seconds) for ``signature`` (defaults to
    the current extractor), or the fall-back when no matching runs
    have been recorded yet.

    Averages the last ``_SAMPLE_SIZE`` matching runs. Each run
    contributes ``elapsed_s / n_pdfs``; the sample mean is returned."""
    sig = signature if signature is not None else _current_signature()
    matching = [r for r in _load_history() if r.signature == sig]
    if not matching:
        return FALLBACK_SECONDS_PER_PDF
    recent = matching[-_SAMPLE_SIZE:]
    per_pdf_samples = [r.elapsed_s / r.n_pdfs for r in recent if r.n_pdfs > 0]
    if not per_pdf_samples:
        return FALLBACK_SECONDS_PER_PDF
    return sum(per_pdf_samples) / len(per_pdf_samples)


def estimate_seconds_for(n_pdfs: int, signature: str | None = None) -> float:
    """Convenience: ETA for ``n_pdfs`` files using the calibrated
    per-PDF average for ``signature`` (defaults to the current
    extractor)."""
    if n_pdfs <= 0:
        return 0.0
    return float(n_pdfs) * estimate_per_pdf_seconds(signature=signature)


def format_duration(seconds: float) -> str:
    """Human-readable duration. Used by every confirm screen that
    shows ``Estimated time: …``."""
    if seconds < 1:
        return "a moment"
    if seconds < 60:
        return f"~{int(seconds)} s"
    if seconds < 3600:
        return f"~{int(seconds / 60)} min"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"~{h} h {m} min" if m else f"~{h} h"


def has_calibration_data(signature: str | None = None) -> bool:
    """True when at least one run for ``signature`` (default: current
    extractor) has been persisted. Disclosure copy can use this to
    caveat the estimate as a fall-back."""
    sig = signature if signature is not None else _current_signature()
    return any(r.signature == sig for r in _load_history())


def _load_history() -> list[ThroughputRecord]:
    """Read the persisted jsonl, retagging legacy entries on read.

    Entries without a ``signature`` field were written before signature
    tracking landed. Per the migration spec we treat those as
    flat-only runs under the current cfg, so any pre-pdf-structure
    baseline survives for future flat runs."""
    path = _state_path()
    if not path.exists():
        return []
    legacy_sig = _legacy_signature()
    out: list[ThroughputRecord] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                with contextlib.suppress(KeyError, TypeError, ValueError):
                    sig = data.get("signature")
                    if not isinstance(sig, str) or not sig:
                        sig = legacy_sig
                    out.append(
                        ThroughputRecord(
                            completed_at=float(data["completed_at"]),
                            n_pdfs=int(data["n_pdfs"]),
                            cache_hits=int(data["cache_hits"]),
                            cache_misses=int(data["cache_misses"]),
                            elapsed_s=float(data["elapsed_s"]),
                            signature=sig,
                        )
                    )
    except OSError:
        return []
    return out


__all__ = [
    "FALLBACK_SECONDS_PER_PDF",
    "ThroughputRecord",
    "estimate_per_pdf_seconds",
    "estimate_seconds_for",
    "format_duration",
    "has_calibration_data",
    "record_run",
]
