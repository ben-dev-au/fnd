"""PDF extractor: one chunk per page, with TOC-first heading detection.

Flow:

1. Try ``doc.get_toc()`` first — if the PDF has an embedded outline, that's
   ~100% accurate; map each page to the nearest preceding TOC entry.
2. Else fall back to local font-size clustering (``_font_clustering_heading``).
3. Apply sanity gates and bail to ``heading_path = ""`` when:
   - ≤1 distinct rounded font size (likely OCR'd; clustering yields garbage)
   - >30% of spans flagged as headings (false positives dominate)
   - Page is slide-shaped (landscape ~10:7.5 + sparse text)

When heading_path can't be derived, the chunk still ranks via body/title/path
and the user navigates by page number.

When the optional ``pdf-structure`` extra is installed (pymupdf4llm +
docling), a parallel structured-extraction path runs and populates
``body_md`` for Markdown-rendered preview.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import re
import threading
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any, Final, cast

import pymupdf  # type: ignore[import-not-found]

from fnd.cache import ExtractionCache, sha256_file
from fnd.extract.base import Block, Chunk, ExtractError
from fnd.extract.recovery import (
    CoverageEvaluator,
    ExtractionContext,
    InvisibleTextTier,
    PageRecoveryPipeline,
    ProductionLayoutTier,
)

# Lazy availability of the pdf-structure extra (`pymupdf4llm`). Computed
# at module load — cheap; just a spec lookup. The actual import happens
# inside _extract_page_md() to keep import-time cost zero for users
# who haven't opted in (NF7 invariant).
#
# Note: ``find_spec`` only tests importability, not functional
# readiness. A half-uninstalled pymupdf4llm whose namespace directory
# survives but whose ``to_markdown`` is gone will still pass this
# check. ``_extract_page_md`` is defensive — its outer try/except
# catches the AttributeError that surfaces from such a state and
# returns "" (the page falls through to flat extraction). The TUI's
# Status row uses the stricter ``fnd.extras.is_package_installed``
# (which calls ``importlib.invalidate_caches`` + verifies
# ``spec.origin`` exists) to report install state to the user.
_HAS_PYMUPDF4LLM: bool = importlib.util.find_spec("pymupdf4llm") is not None

# Runtime override for the structured-extraction path. The indexer sets
# this to True for a single Update index run when the user has toggled
# "Update cache at index time" Off — extract() still consults the cache
# on hit, but on miss it runs only the flat path (no pymupdf4llm,
# no docling) and skips the cache write. Used for fast battery-friendly
# flat-text refreshes. Default False keeps existing behaviour.
_skip_structure_extraction: bool = False


def set_skip_structure_extraction(skip: bool) -> None:
    """Toggle the run-scoped flag. The indexer reads
    ``cfg.defaults.cache_at_index_time`` and calls this before
    iterating files; resets it at end of run."""
    global _skip_structure_extraction
    _skip_structure_extraction = skip


# Coarse texture-engine version. The structure-cache key is keyed on this,
# NOT on a per-flag config hash, so a routine app update no longer orphans
# every cached texturising. Bump ONLY for a change that meaningfully alters
# texturised output corpus-wide (a major pymupdf4llm/docling upgrade, a real
# extraction-logic change). Minor refactors, flag documentation, and
# patch-level dependency bumps must NOT bump it. "Re-texturise outdated
# documents" keys off this version: entries below it read as outdated.
TEXTURE_VERSION: Final[int] = 1


def texture_signature() -> str:
    """Cache-key signature: coarse and manually-versioned. See
    :data:`TEXTURE_VERSION`. Distinct from :func:`extractor_signature`,
    which stays fine-grained for ETA-calibration cohorts."""
    return f"tex-v{TEXTURE_VERSION}"


# Run-scoped "re-texturise outdated" flag. When True, extract() reuses ONLY a
# current-signature cache entry and otherwise re-extracts fresh — so a
# Re-texturise-outdated pass actually upgrades pre-version texturising. When
# False (default), extract() reuses any prior entry for the same content,
# making texturising durable across a TEXTURE_VERSION bump.
_force_fresh_texture: bool = False


def set_force_fresh_texture(value: bool) -> None:
    """Toggle the run-scoped re-texturise flag; reset at end of run."""
    global _force_fresh_texture
    _force_fresh_texture = value


# Regex for the pymupdf4llm "couldn't decode this region" marker. When a
# whole table is embedded as a raster image (common in HBR / finance
# PDFs), pymupdf4llm emits a literal "==> picture [W x H] intentionally
# omitted <==" instead of the cell values. We use this to detect
# pages that need a docling fallback for the missing structure.
_PIC_OMITTED_RE = re.compile(r"==>\s*picture\s*\[(\d+)\s*x\s*(\d+)\]\s*intentionally omitted\s*<==")

# Trigger fallback when the omitted-image region is at least this
# fraction of the page area. Below ~15% it's typically a logo / figure /
# headshot rather than a table; not worth paying the docling cost.
_FALLBACK_AREA_RATIO = 0.15

# Secondary signal: a literal "TABLE N" / "Table N" heading on the same
# page as a picture-omitted marker means the picture IS a table even if
# the region itself is below the area threshold (HBR-style narrow tables).
_TABLE_LABEL_RE = re.compile(r"\b(?:TABLE|Table)\s+\d", re.MULTILINE)

# A Markdown table row: one or more `| ... |` lines. Copy of
# dev/tools/pdf_bakeoff/metrics._TABLE_ROW_RE (fnd must not import the
# dev tree); keep the two in sync.
_TABLE_LINE_RE = re.compile(r"^\s*\|.+\|\s*$")


def _parent_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


def _margin_integers(page: pymupdf.Page) -> list[int]:
    """Every plausible integer found in the top or bottom 12% margin
    of ``page``. Used as candidates for the running page number; the
    cross-page resolver in :func:`_resolve_page_labels` decides which
    candidate (if any) is actually the printed page number.

    We're permissive on purpose — a header like ``"Chapter 5    291"``
    yields both ``5`` and ``291`` here, and the cross-page sequence
    check then picks ``291`` because that's the integer that ticks up
    by 1 across consecutive pages. The same logic naturally rejects
    chapter numbers (which stay constant across many pages) and
    section numbers (non-monotonic).
    """
    rect = page.rect
    if rect.height <= 0:
        return []
    margin = rect.height * 0.12
    top_max = margin
    bot_min = rect.height - margin

    text_dict = cast(dict[str, Any], page.get_text("dict"))
    out: list[int] = []
    for block in text_dict.get("blocks", []):
        bbox = block.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        y_top = float(bbox[1])
        y_bot = float(bbox[3])
        if not (y_bot < top_max or y_top > bot_min):
            continue
        text_parts: list[str] = []
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = (span.get("text") or "").strip()
                if t:
                    text_parts.append(t)
        block_text = " ".join(text_parts)
        for m in re.finditer(r"\b(\d{1,5})\b", block_text):
            n = int(m.group(1))
            if 1 <= n <= 99999:
                out.append(n)
    return out


def _resolve_page_labels(
    *,
    meta_labels: list[str],
    margin_candidates: list[list[int]],
    min_run: int = 3,
) -> list[str]:
    """Decide a printed page label per page index.

    Two-stage:

    1. Pages whose ``meta_labels[i]`` is non-empty keep that — the
       PDF declared an explicit label, which is exact.
    2. For the rest we scan ``margin_candidates`` for the longest
       *consecutive* sequence: pages ``i, i+1, ..., i+k`` whose
       margin yielded ``v, v+1, ..., v+k`` for some starting value
       ``v``. That's almost certainly the running page number. Pages
       inside the run get ``str(v + offset)``; pages outside get
       ``""`` and the display layer falls back to the PDF index.

    A run shorter than ``min_run`` is rejected as too coincidental
    to trust.
    """
    n = len(meta_labels)
    out = list(meta_labels)
    if n == 0:
        return out

    pending = [i for i in range(n) if not out[i]]
    if len(pending) < min_run:
        return out

    sets: list[set[int]] = [set(margin_candidates[i]) for i in range(n)]

    best_start_index = -1
    best_start_value = 0
    best_length = 0
    pending_set = set(pending)
    for start_pos, idx0 in enumerate(pending):
        # Only try as run-start values from this page's candidates.
        for v0 in sets[idx0]:
            length = 1
            cursor = start_pos + 1
            expected_idx = idx0 + 1
            expected_v = v0 + 1
            while cursor < len(pending):
                nxt_idx = pending[cursor]
                if nxt_idx != expected_idx:
                    break
                if expected_v not in sets[nxt_idx]:
                    break
                length += 1
                cursor += 1
                expected_idx += 1
                expected_v += 1
            if length > best_length:
                best_length = length
                best_start_index = idx0
                best_start_value = v0

    if best_length < min_run:
        return out

    # Apply the run.
    for k in range(best_length):
        idx = best_start_index + k
        if idx in pending_set:
            out[idx] = str(best_start_value + k)
    return out


def _toc_heading_for_page(toc: list[list[object]], page_no_1based: int) -> str:
    """Walk the TOC and return ``A > B > C`` for the deepest entry whose page
    number is ``<= page_no_1based``. Empty if no entry applies."""
    stack: list[tuple[int, str]] = []  # (level, title)
    last_match_path: str = ""
    for entry in toc:
        # toc entries are [level, title, page, ...]; pymupdf returns object-typed.
        if not entry:
            continue
        level = int(cast(int, entry[0]))
        title = str(entry[1]) if len(entry) > 1 else ""
        page = int(cast(int, entry[2])) if len(entry) > 2 else 0
        if page == 0:
            continue
        if page > page_no_1based:
            break
        stack[level - 1 :] = [(level, title)]
        last_match_path = " > ".join(t for _, t in stack)
    return last_match_path


def _is_slide_shape(rect: pymupdf.Rect, span_count: int) -> bool:
    """Heuristic: landscape aspect ratio + few text spans."""
    w, h = float(rect.width), float(rect.height)
    if h == 0:
        return False
    ratio = w / h
    return ratio > 1.2 and span_count < 60


def _font_clustering_heading(
    page: pymupdf.Page,
) -> tuple[str, str]:
    """Return ``(heading_text_for_page, page_title)`` from font-size clustering.

    Bail (return ``("", "")``) when sanity gates trip. Title-on-slide handling
    falls out naturally because slides are detected upstream.
    """
    text_dict = cast(dict[str, Any], page.get_text("dict"))
    spans: list[tuple[float, str, int]] = []  # (rounded_size, text, flags)
    for block in text_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text") or "").strip()
                if not txt:
                    continue
                size = round(float(span.get("size", 0.0)))
                spans.append((float(size), txt, int(span.get("flags", 0))))

    if not spans:
        return ("", "")

    # Sanity gate 1: ≤1 distinct font size → likely OCR'd.
    distinct_sizes = {s for s, _, _ in spans}
    if len(distinct_sizes) <= 1:
        return ("", "")

    sizes_sorted = sorted(distinct_sizes, reverse=True)
    body_size = max(distinct_sizes, key=lambda s: sum(1 for x in spans if x[0] == s))

    candidates: list[str] = []
    for size, txt, _flags in spans:
        if size > body_size and len(txt) <= 120 and not txt.endswith((".", "!", "?")):
            candidates.append(txt)

    # Sanity gate 2: >30% of spans flagged as headings → too noisy.
    if len(candidates) > 0.30 * len(spans):
        return ("", "")

    # The single largest distinct size on the page is the page-level heading.
    largest = sizes_sorted[0]
    page_headings = [t for s, t, _ in spans if s == largest and len(t) <= 120]
    page_title = page_headings[0] if page_headings else ""
    heading_text = page_title
    return (heading_text, page_title)


@contextlib.contextmanager
def _mute_fd(fd: int) -> Generator[None]:
    """Redirect a file descriptor to /dev/null for the block's duration.

    Used to silence libmupdf's stdout banner ("=== Document parser
    messages === / Using Tesseract for OCR processing.") emitted from
    C-level code during pymupdf4llm extraction. Not catchable via
    contextlib.redirect_stdout.
    """
    saved = os.dup(fd)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, fd)
        yield
    finally:
        os.dup2(saved, fd)
        os.close(saved)
        os.close(null)


def _try_docling_fallback(pdf_path: str, page_index: int) -> str:
    """Try to extract one page via the docling daemon. Returns "" on
    any failure — caller keeps whatever pymupdf4llm produced."""
    try:
        from fnd.extract._docling_daemon import DoclingDaemon
    except Exception:
        return ""
    daemon = DoclingDaemon.get()
    if daemon is None:
        return ""
    try:
        return daemon.extract_page(Path(pdf_path), page_index)
    except Exception:
        return ""


def _extract_md_tables(md: str) -> list[str]:
    """Each contiguous Markdown table block in `md`, in document order.

    A block is one or more consecutive `| ... |` rows (mirrors
    metrics.count_tables' contiguity rule)."""
    blocks: list[str] = []
    cur: list[str] = []
    for line in md.splitlines():
        if _TABLE_LINE_RE.match(line):
            cur.append(line)
        elif cur:
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _splice_docling_tables(pymupdf_md: str, docling_md: str) -> str:
    """Merge docling's recovered tables into pymupdf4llm's formatted page.

    pymupdf4llm keeps inline formatting (bold/italic/headings) but emits
    a `==> picture omitted <==` marker where it couldn't decode an
    image-rendered table; docling recovers the table but drops the
    formatting. docling only earns its place when pymupdf4llm *missed* a
    table — so:

    - docling found no table (the marker is a genuine figure/chart):
      keep pymupdf4llm verbatim. No table to gain, so don't pay the
      formatting cost of replacing the page.
    - pymupdf4llm already rendered a table on this page (a vector table):
      keep pymupdf4llm. The marker is a redundant image of a table it
      already has; splicing docling's copy would duplicate it. (Rare
      limitation: a page where pymupdf got table A but missed table B
      behind the marker keeps only A — measured as not occurring.)
    - pymupdf4llm has no table and docling found one per marker:
      replace each marker line (and the ``**`` wrapper pymupdf4llm puts
      it in) with its table, in reading order. Line-based, so no index
      shifting and a marker line is swapped cleanly.
    - no table but counts mismatch (placement ambiguous): full-page
      replacement, so a recovered table is never dropped.
    """
    tables = _extract_md_tables(docling_md)
    if not tables:
        return pymupdf_md
    if _extract_md_tables(pymupdf_md):
        return pymupdf_md
    lines = pymupdf_md.splitlines()
    marker_lines = [i for i, ln in enumerate(lines) if _PIC_OMITTED_RE.search(ln)]
    if not marker_lines or len(marker_lines) != len(tables):
        return docling_md
    for i, table in zip(marker_lines, tables, strict=True):
        lines[i] = table
    spliced = "\n".join(lines)
    return spliced + "\n" if pymupdf_md.endswith("\n") else spliced


def _strip_picture_markers(md: str) -> str:
    """Drop pymupdf4llm's `==> picture [W x H] intentionally omitted <==`
    placeholders from the user-facing preview. Run only AFTER the docling
    splice — markers it used to place tables are already replaced, so this
    removes just the leftovers on dedup / figure / non-routed pages and
    never disturbs a recovered table."""
    kept: list[str] = []
    for line in md.splitlines():
        if _PIC_OMITTED_RE.search(line):
            without = _PIC_OMITTED_RE.sub("", line)
            # Marker-only line (incl. the ``**`` wrapper pymupdf4llm adds):
            # drop it entirely; otherwise just excise the marker text.
            if not without.replace("*", "").strip():
                continue
            kept.append(without)
        else:
            kept.append(line)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n")
    return result + "\n" if md.endswith("\n") and result else result


def _needs_docling_fallback(page: pymupdf.Page, pymupdf_md: str) -> bool:
    """Cheap heuristic: did pymupdf4llm visibly miss a structured region?

    Triggers in two cases:
    1. Sum of "picture intentionally omitted" rectangles exceeds
       `_FALLBACK_AREA_RATIO` of the page area — most likely a big
       borderless / image-rendered table.
    2. A picture-omitted marker appears alongside a "TABLE" / "Table"
       label on the same page — catches small image-rendered tables
       (the HBR p99 case: a 324x70 pt = ~5% region that's clearly a
       table per its adjacent "TABLE 5-2" heading).
    """
    if not _PIC_OMITTED_RE.search(pymupdf_md):
        return False
    page_area = float(page.rect.width) * float(page.rect.height)
    if page_area > 0:
        omitted = 0.0
        for w_s, h_s in _PIC_OMITTED_RE.findall(pymupdf_md):
            omitted += float(w_s) * float(h_s)
        if (omitted / page_area) >= _FALLBACK_AREA_RATIO:
            return True
    # Label-proximity signal: "TABLE N" or "Table N" near a picture
    # marker is a strong "this picture is a table" hint.
    return bool(_TABLE_LABEL_RE.search(pymupdf_md))


def _extract_page_md(doc: pymupdf.Document, page_index: int) -> str:
    """Extract one page's Markdown via pymupdf4llm. Returns "" on failure.

    Only called when `_HAS_PYMUPDF4LLM` is True. Config matches the
    Phase 0 bake-off winning settings: vector tables on, OCR off,
    image emission off.
    """
    try:
        pymupdf4llm = importlib.import_module("pymupdf4llm")
    except ImportError:
        return ""
    # Layout mode (auto-enabled when pymupdf-layout is on the path)
    # accepts our no-OCR flag combo; the standard path's validator
    # rejects it. Both modes produce equivalent markdown on our corpus;
    # the only reason to choose one is which validator runs.
    try:
        with _mute_fd(1):
            chunks = pymupdf4llm.to_markdown(
                doc,
                pages=[page_index],
                page_chunks=True,
                show_progress=False,
                force_text=False,
                ignore_images=True,
                ignore_graphics=False,
                table_strategy="lines",
                # Explicit: pymupdf-layout auto-enables use_ocr, which OCRs
                # image pages inline and suppresses the picture-omitted
                # markers the docling fallback routes on.
                use_ocr=False,
            )
    except Exception:
        # pymupdf4llm has stricter input validation than vanilla
        # pymupdf and can reject pages that the rest of the pipeline
        # accepts. Fall through to empty md — caller treats this as
        # "structured mode unavailable for this page" and the chunk
        # still ships its flat body.
        return ""
    if not chunks:
        return ""
    first = chunks[0]
    return str(first.get("text", "")) if isinstance(first, dict) else str(first)


def _extract_invisible_md(doc: pymupdf.Document, page_index: int) -> str:
    """Re-extract one page through the ignore_alpha lever in non-layout
    mode, recovering invisible OCR text the layout parser discards.

    Toggles the process-global ``use_layout`` False for the call and
    restores its prior value in ``finally`` — a worker process handles
    one file's pages sequentially, so the toggle never races. Mutes both
    stdout and stderr (the non-layout path is chattier than production).
    """
    try:
        pymupdf4llm = importlib.import_module("pymupdf4llm")
    except ImportError:
        return ""
    prior = bool(getattr(pymupdf4llm, "_use_layout", True))
    try:
        pymupdf4llm.use_layout(False)
        with _mute_fd(1), _mute_fd(2):
            chunks = pymupdf4llm.to_markdown(
                doc,
                pages=[page_index],
                page_chunks=True,
                show_progress=False,
                ignore_alpha=True,
                force_text=True,
                ignore_images=True,
                ignore_graphics=False,
                table_strategy="lines",
            )
    except Exception:
        return ""
    finally:
        pymupdf4llm.use_layout(prior)
    if not chunks:
        return ""
    first = chunks[0]
    return str(first.get("text", "")) if isinstance(first, dict) else str(first)


_recovery_pipeline_singleton: PageRecoveryPipeline | None = None


def _recovery_pipeline() -> PageRecoveryPipeline:
    """The composed page-recovery pipeline (built once). Stateless tiers,
    so a module-level singleton avoids per-page allocation."""
    global _recovery_pipeline_singleton
    if _recovery_pipeline_singleton is None:
        _recovery_pipeline_singleton = PageRecoveryPipeline(
            [
                ProductionLayoutTier(_extract_page_md),
                InvisibleTextTier(_extract_invisible_md, CoverageEvaluator()),
            ]
        )
    return _recovery_pipeline_singleton


def _config_hash() -> str:
    """Hash of config-shaping flags; any tuning change bumps the key."""
    config = {
        "force_text": False,
        "ignore_images": True,
        "ignore_graphics": False,
        "table_strategy": "lines",
        "use_ocr": False,
        "fallback_area_ratio": _FALLBACK_AREA_RATIO,
        "table_label_re": _TABLE_LABEL_RE.pattern,
        "docling_merge": "splice-dedup",
        "strip_picture_markers": True,
        # Bug-E invisible-text recovery: bumping these re-textures scanned
        # books so the dropped OCR layer is recovered on next reindex.
        "invisible_text_fallback": "ignore_alpha",
        "cov_gate": 0.70,
        "min_flat_tokens": 20,
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()[:8]


def extractor_signature() -> str:
    """Stable string identifying this extractor's behaviour.

    Different signature → different cache key, and different throughput
    cohort for ETA calibration. Captures:
    - flat vs structured path (gated by pymupdf4llm availability)
    - whether docling fallback is wired in (gated by `docling` CLI)
    - config flag hash so a future tuning change forces re-extraction
    """
    parts = ["flat"]
    if _HAS_PYMUPDF4LLM:
        try:
            pymupdf4llm = importlib.import_module("pymupdf4llm")
            ver = getattr(pymupdf4llm, "__version__", "unknown")
        except ImportError:
            ver = "unknown"
        parts = [f"pymupdf4llm-{ver}"]
        if _has_docling():
            parts.append("docling")
    parts.append(f"cfg-{_config_hash()}")
    return "|".join(parts)


def legacy_flat_signature() -> str:
    """Signature for flat-only extraction under the current cfg.

    Used by ETA calibration to retag un-tagged history entries (which
    predate signature tracking) so a user who indexed before installing
    ``pdf-structure`` keeps their fast-machine baseline for flat runs.
    """
    return "|".join(["flat", f"cfg-{_config_hash()}"])


# Back-compat private alias. Existing call sites in this module use the
# underscored name; keep them working without churning surrounding code.
_extractor_signature = extractor_signature


def _has_docling() -> bool:
    """Cached check for docling CLI presence — used by signature only."""
    import shutil

    return shutil.which("docling") is not None


def extract(
    path: Path,
    *,
    on_heartbeat: Callable[[Any], None] | None = None,
) -> Iterator[Chunk]:
    """Extract PDF chunks, consulting the on-disk cache first.

    On cache hit: yields chunks directly from the JSON blob, skipping
    the multi-second pymupdf4llm + docling work entirely.

    On miss: runs the normal extraction, then writes the result back
    to the cache before yielding (so a Ctrl+C *after* the put is
    safe — next reindex will hit).

    ``on_heartbeat`` receives per-page ("page", index) tuples plus a
    ("total", N) opener and ("file-start", path) reset. Used by the
    indexer runner's live-progress channel so the IndexerScreen ETA
    refines as a single long PDF processes its pages.
    """
    cache = _get_cache()
    try:
        content_sha = sha256_file(path)
    except OSError as e:
        raise ExtractError(str(path), f"cannot read for hash: {e}") from e
    key = cache.build_key(content_sha256=content_sha, extractor_signature=texture_signature())
    cached = cache.get(key)
    if cached is None and not _force_fresh_texture:
        # Durable reuse: a TEXTURE_VERSION bump or a pre-versioning entry
        # left the current key empty, but this file's texturising still
        # exists under an older key. Reuse it instead of redoing the work.
        # (Re-texturise-outdated mode sets _force_fresh_texture, skipping
        # this so it genuinely re-extracts under the current signature.)
        cached = cache.get_any_for_content(content_sha)
    if cached is not None:
        # Cache entries are keyed by content hash, so two different files
        # with identical bytes share an entry. The chunks were captured
        # with the original file's parent_id / path / mtime — overlay
        # the *current* file's identity so downstream indexer code
        # routes chunks to the right Tantivy parent_id.
        parent_id_now = _parent_id(path)
        path_str_now = str(path)
        try:
            mtime_now = int(path.stat().st_mtime)
        except OSError:
            mtime_now = cached[0].mtime if cached else 0
        for chunk in cached:
            chunk.parent_id = parent_id_now
            chunk.path = path_str_now
            chunk.mtime = mtime_now
            yield chunk
        return

    # Dispatch the heavy extraction to a subprocess. pymupdf-layout
    # holds the GIL across long pages; running it in-thread (even via
    # asyncio.to_thread) starves the main asyncio loop and the user
    # sees a frozen UI. The subprocess pool gives the worker its own
    # GIL so the caller's event loop stays responsive throughout.
    #
    # The worker emits a per-page heartbeat; the parent kills the
    # worker if no heartbeat arrives for ``stall_seconds``. This is
    # the safety net for an unattended index hitting a genuinely hung
    # native call. The threshold is large enough that any legitimately
    # slow but progressing PDF (image-dense pages, docling fallback)
    # stays alive.
    from fnd.extract._worker import (
        StallError,
        collect_pdf_chunks_with_heartbeat,
        run_in_pool_sync_with_stall_detection,
    )

    try:
        chunks = run_in_pool_sync_with_stall_detection(
            collect_pdf_chunks_with_heartbeat,
            path,
            _skip_structure_extraction,
            stall_seconds=120.0,
            first_beat_grace_seconds=180.0,
            on_heartbeat=on_heartbeat,
        )
    except StallError as e:
        raise ExtractError(str(path), f"extractor wedged: {e}") from e
    except ExtractError:
        raise
    except Exception as e:
        # pymupdf is a C-extension parser with a long CVE history; any
        # Python-level exception from inside it (UAF surfacing as
        # `RuntimeError`, malformed-stream `ValueError`, ...) becomes
        # an ExtractError so the index build survives.
        raise ExtractError(str(path), f"{type(e).__name__}: {e}") from e

    # Best-effort cache write; if it fails (disk full, perms) we still
    # yield the freshly-extracted chunks — caller should never lose work
    # because the cache had a bad day. Skipped when the run-scoped
    # battery-saver flag is on (no point caching flat extractions —
    # they're already cheap to recompute and cache hits assume
    # structured chunks).
    if not _skip_structure_extraction:
        with contextlib.suppress(OSError):
            cache.put(key, chunks)

    yield from chunks


def _get_cache() -> ExtractionCache:
    """Cached singleton — building the path each call is cheap but
    creating the directory tree once at first use is cleaner.

    The prefetcher and indexer can both reach this from background
    threads, so guard the check-and-set with a lock to avoid two
    instances racing into existence."""
    global _cache_singleton
    if _cache_singleton is not None:
        return _cache_singleton
    with _cache_lock:
        if _cache_singleton is None:
            _cache_singleton = ExtractionCache()
    return _cache_singleton


_cache_singleton: ExtractionCache | None = None
_cache_lock = threading.Lock()


# Invoked from the subprocess pool via fnd.extract._worker.collect_pdf_chunks,
# which looks the function up dynamically; pyright can't see that call.
def _extract_inner(  # pyright: ignore[reportUnusedFunction]
    path: Path,
    *,
    on_page: Callable[[int], None] | None = None,
) -> Iterator[Chunk]:
    parent_id = _parent_id(path)
    mtime = int(path.stat().st_mtime)

    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        raise ExtractError(str(path), f"pymupdf cannot open: {e}") from e
    try:
        if doc.is_encrypted or doc.needs_pass:
            raise ExtractError(str(path), "encrypted PDF (password required)")
        meta = doc.metadata or {}
        meta_title = str(meta.get("title") or "")
        meta_author = str(meta.get("author") or "")
        toc: list[list[object]] = doc.get_toc() or []

        # First pass: build per-page state and collect label
        # candidates. We need every page's margin integers up front so
        # the cross-page resolver in ``_resolve_page_labels`` can find
        # the consecutive run that pins down the printed numbering.
        # Pages with no body text are kept as ``None`` to preserve
        # ``page_index`` alignment with the resolver's output.
        page_states: list[dict[str, Any] | None] = []
        meta_labels: list[str] = []
        margin_candidates: list[list[int]] = []
        for page_index in range(doc.page_count):
            # Heartbeat for the parent's stall detector. Fires before
            # the heavy structured-extraction call on this page so a
            # wedge inside a single page is still detectable.
            if on_page is not None:
                on_page(page_index)
            page = doc[page_index]
            page_no = page_index + 1
            try:
                meta_label = page.get_label() or ""
            except Exception:
                meta_label = ""
            meta_labels.append(meta_label)
            margin_candidates.append(_margin_integers(page))

            text = cast(str, page.get_text("text") or "")
            if not text.strip():
                page_states.append(None)
                continue

            # Heading: TOC-first, then font clustering, then empty.
            heading_path = ""
            page_title = ""
            if toc:
                heading_path = _toc_heading_for_page(toc, page_no)
                page_title = heading_path.split(" > ")[-1] if heading_path else ""

            if not heading_path:
                # Slide-shape sanity gate before we even cluster.
                td = cast(dict[str, Any], page.get_text("dict"))
                span_count = sum(
                    1
                    for b in td.get("blocks", [])
                    for ln in b.get("lines", [])
                    for sp in ln.get("spans", [])
                    if (sp.get("text") or "").strip()
                )
                if not _is_slide_shape(page.rect, span_count):
                    h, t = _font_clustering_heading(page)
                    heading_path = h
                    page_title = t

            blocks: list[Block] = []
            if page_title:
                blocks.append(Block(kind="h2", text=page_title))
            blocks.append(Block(kind="p", text=text.strip()))

            # Structured path (opt-in): populate body_md so the preview
            # dispatcher routes this page to the Markdown widget. Empty
            # when the pdf-structure extra isn't installed — keeps
            # behaviour byte-identical to today. Also skipped when the
            # run-scoped ``_skip_structure_extraction`` flag is set
            # (battery-saver mode).
            structure_on = _HAS_PYMUPDF4LLM and not _skip_structure_extraction
            if structure_on:
                ctx = ExtractionContext(
                    doc=doc,
                    page=page,
                    page_index=page_index,
                    path=str(path),
                    flat=text,
                )
                body_md = _recovery_pipeline().recover(ctx).markdown
            else:
                body_md = ""

            # Phase 3 routing: if pymupdf4llm visibly missed structure
            # (e.g. a big image-rendered table), try docling as a
            # fallback. Docling's ML layout model can recover those.
            # Splice the recovered table(s) into pymupdf4llm's formatted
            # page rather than replacing it wholesale (which would drop
            # bold/italic/headings, and on figure pages gain nothing).
            # No-op when docling isn't installed.
            if body_md and _needs_docling_fallback(page, body_md):
                docling_md = _try_docling_fallback(str(path), page_index)
                if docling_md:
                    body_md = _splice_docling_tables(body_md, docling_md)

            # Strip any leftover picture-omitted placeholders from the
            # preview — done after routing/splice so the markers that
            # positioned recovered tables are already gone, and multi-table
            # pages are unaffected.
            if body_md:
                body_md = _strip_picture_markers(body_md)

            page_states.append(
                {
                    "page_no": page_no,
                    "page_index": page_index,
                    "text": text,
                    "blocks": blocks,
                    "body_md": body_md,
                    "heading_path": heading_path,
                }
            )

        # Resolve labels using meta + cross-page sequence detection.
        labels = _resolve_page_labels(
            meta_labels=meta_labels,
            margin_candidates=margin_candidates,
        )

        for state in page_states:
            if state is None:
                continue
            yield Chunk(
                parent_id=parent_id,
                path=str(path),
                mtime=mtime,
                kind="pdf",
                body=state["text"],
                body_struct=state["blocks"],
                body_md=state["body_md"],
                page=state["page_no"],
                page_label=labels[state["page_index"]],
                heading_path=state["heading_path"],
                title=meta_title,
                author=meta_author,
                chunk_seq=state["page_index"],
            )
    finally:
        doc.close()
