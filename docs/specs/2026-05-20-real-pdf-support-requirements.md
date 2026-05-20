# Real PDF Support — Requirements & Validation

**Companion to:** `docs/specs/2026-05-20-real-pdf-support.md` (design),
`docs/plans/2026-05-20-real-pdf-support-bakeoff.md` (phased plan).

Each requirement gets a stable ID, a one-line acceptance test, and a
column tracking implementation + verification status. Updated as Phase
1+ work lands.

## Status legend

- `—`     not started
- `wip`   in progress
- `done`  implemented
- `test`  has at least one automated test
- `verif` manually verified end-to-end on real corpus

## Functional requirements

| ID | Requirement | Test | Status |
|---|---|---|---|
| F1 | Without `pdf-structure` extra installed, `fnd` works byte-identically to today | `tests/test_pdf_extras_optional.py::test_extract_without_pymupdf4llm` | wip |
| F2 | `fnd extras list` shows available extras + installed status | `tests/test_extras_cli.py::test_list` | — |
| F3 | `fnd extras install pdf-structure` runs `uv pip install fnd[pdf-structure]` + `uv tool install docling-slim[standard]` after disclosure prompt | `tests/test_extras_cli.py::test_install_dry_run` | — |
| F4 | `fnd extras uninstall pdf-structure` removes both packages + cached ML weights after prompt | `tests/test_extras_cli.py::test_uninstall_dry_run` | — |
| F5 | With extras installed, PDF chunks have populated `body_md` | `tests/test_pdf_extract_structured.py::test_body_md_populated` | — |
| F6 | With extras installed, image-table pages trigger docling fallback | `tests/test_pdf_extract_structured.py::test_docling_fallback_on_omitted_picture` | — |
| F7 | `"pdf"` is in `_MARKDOWN_RENDERED_KINDS`; PDFs with non-empty `body_md` route to structural preview | `tests/test_preview_dispatcher.py::test_pdf_with_body_md_routes_structural` | — |
| F8 | PDFs with empty `body_md` (no extras path) route to flat preview | existing `test_preview_dispatcher.py` (no change needed) | done |
| F9 | After uninstall, previously-indexed structured chunks still render (markdown is in the index, not extractor) | `tests/test_extras_cli.py::test_uninstall_preserves_indexed_chunks` | — |
| F10 | docling daemon spawned lazily, reused across the reindex, torn down at end | `tests/test_extract_pdf_docling_daemon.py::test_daemon_lifecycle` | — |

## Non-functional requirements

| ID | Requirement | Verification | Status |
|---|---|---|---|
| NF1 | Zero behavioural change for users not opting in | Snapshot test: PDF extraction output without pymupdf4llm matches today's output | wip |
| NF2 | Install/uninstall prompts disclose disk size + network impact before any download | Manual: dry-run prompts, capture stderr text | — |
| NF3 | Install fails cleanly if user declines prompt (no half-installed state) | `tests/test_extras_cli.py::test_install_aborted_leaves_no_artifacts` | — |
| NF4 | docling fallback failure (crash, timeout, missing) falls back to pymupdf4llm output with a warning, never propagates | `tests/test_extract_pdf_docling_daemon.py::test_daemon_crash_falls_back` | — |
| NF5 | Per-page extraction: pymupdf4llm <0.25s, docling <0.6s on born-digital A4 (M1 Max baseline) | Benchmark via existing `tools/pdf_bakeoff/` harness | verif (Phase 0) |
| NF6 | `fnd extras status` accurately reports disk usage within ±10% of actual | Manual: compare reported vs `du -sh` output | — |
| NF7 | No pymupdf4llm/docling imports happen at fnd startup when extra is absent (lazy-load) | `tests/test_pdf_extras_optional.py::test_no_lazy_import_at_startup` | — |

## Cross-cutting invariants

These should hold across every Phase 1+ commit:

- **Lint clean**: `make lint` passes
- **Existing tests pass**: `make test` does not regress
- **No co-author trailers** in commits (per project memory)
- **Memory-aware**: no AI/Claude references in code, docs, paths, commits

## Validation method

After each step lands:

1. Unit tests for new code paths added in that step
2. Regression run of `make test` (full suite, accepting documented flakes)
3. Manual smoke against `tests/fixtures/papers/test.pdf` for both extras-present and extras-absent modes
4. End-of-Phase-1: manual smoke against a real Documents/Readings PDF that needs the docling fallback (e.g., the HBR Entrepreneur's Handbook with TABLE 5-2)
