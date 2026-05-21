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
| F1 | Without `pdf-structure` extra installed, `fnd` works byte-identically to today | `tests/test_pdf_extras_optional.py` (4 tests) | test |
| F2 | `fnd extras list` shows available extras + installed status | `tests/test_extras_cli.py::test_extras_list_shows_pdf_structure` | test |
| F3 | `fnd extras install pdf-structure` runs install commands after disclosure prompt | `tests/test_extras_cli.py::test_install_dry_run_discloses_disk_and_network` | test |
| F4 | `fnd extras uninstall pdf-structure` removes both packages + cached ML weights after prompt | `tests/test_extras_cli.py::test_uninstall_dry_run_discloses_removed_packages` | test |
| F5 | With extras installed, PDF chunks have populated `body_md` | `tests/test_pdf_extract_structured.py` (3 tests) | test |
| F6 | With extras installed, image-table pages trigger docling fallback | implemented in `_needs_docling_fallback` + `_try_docling_fallback` | done (manual verif pending) |
| F7 | `"pdf"` in `_MARKDOWN_RENDERED_KINDS`; PDFs with non-empty `body_md` → structural preview | `tests/test_preview_dispatcher.py::test_pdf_with_body_md_takes_structural_path` | test |
| F8 | PDFs with empty `body_md` → flat preview | `tests/test_preview_dispatcher.py::test_pdf_chunks_take_flat_path` | test |
| F9 | After uninstall, previously-indexed structured chunks still render | implemented (index untouched on uninstall); UI test pending | done (test pending) |
| F10 | docling daemon spawned lazily, reused across reindex, torn down at exit | `tests/test_pdf_docling_daemon.py` (lifecycle tests) | test |
| F11 | identical file content → cache hit | `tests/test_extraction_cache.py::test_put_then_get_round_trip` | test |
| F12 | same content, different extractor → cache miss | `tests/test_extraction_cache.py::test_build_key_differs_per_extractor` | test |
| F13 | cache write is atomic (Ctrl+C safe) | `tests/test_extraction_cache.py::test_put_atomic_no_partial_file_on_failure` | test |
| F14 | corrupt entry → silent miss + re-extract | `tests/test_extraction_cache.py::test_get_corrupt_json_returns_none` | test |
| F15 | schema_version mismatch → silent miss | `tests/test_extraction_cache.py::test_get_schema_version_mismatch_returns_none` | test |
| FCLI | `fnd cache status/clear/prune/info` | `tests/test_cache_cli.py` (7 tests) | test |
| F16 | `start_indexer` from TUI / palette opens IndexerScreen | `IndexerScreen` class + smoke via `start_indexer()` | done |
| F17 | Background mode dismisses modal but keeps task running | `IndexerScreen.action_background()` + task on app | done |
| F18 | Quit mid-reindex leaves state file; reopen auto-resumes (no prompt) | `tests/test_index_runner.py` + `_maybe_resume_indexer` | test |
| F19 | Cancel clears state file so next launch doesn't auto-resume | `tests/test_index_runner.py::test_run_indexer_clears_state_file_on_done` | test |
| F20 | Cache-hit files reported on `file_complete` event | `tests/test_index_runner.py::test_cache_hit_reported_on_warm_run` | test |
| F21 | First-reindex warning shown once then suppressed | `tests/test_first_reindex_warning.py` (10 tests) | test |
| F22 | `defaults.indexer_auto_resume = false` suppresses auto-resume on launch | `_maybe_resume_indexer` reads config flag; declared on Defaults | done |
| F23 | Settings → Indexing surfaces structured PDF status (●/○) + disk | `tests/test_settings_structured_pdf.py::test_status_row_not_installed` | test |
| F24 | Settings → Indexing → Install / Uninstall pushes confirm screen with disclosure | `tests/test_settings_structured_pdf.py::test_install_confirm_chrome_when_not_installed` | test |
| F25 | Confirm → Yes runs install/uninstall in IndexerScreen-style progress modal | `tests/test_settings_extras_install_progress.py::test_progress_modal_chrome` | test |
| F26 | Esc / Background dismisses progress modal but task survives | `tests/test_settings_extras_install_progress.py::test_background_keeps_task_running` | test |
| F27 | Cancel SIGTERMs the active subprocess | `tests/test_settings_extras_install_progress.py::test_cancel_stops_subprocess` | test |
| F28 | Auto-resume toggle in Settings → Indexing → Reindex behaviour | `tests/test_settings_indexing.py::test_indexing_toggle_via_enter` | test |
| F29 | Cache size row shows live entry count + bytes | `tests/test_settings_cache_maintenance.py::test_cache_size_row_shows_count_and_size` | test |
| F30 | Cache maintenance: Prune stale + Clear cache with confirm | `tests/test_settings_cache_maintenance.py::test_clear_yes_path` / `test_prune_with_stale_opens_confirm` | test |
| F31 | Search `/auto-resume`, `/pdf-structure`, `/cache`, `/prune` finds correct rows | `tests/test_settings_indexing_search.py` (parametrized) | test |
| F32 | Confirm dialogs accept ↑↓ navigation; Enter selects; Esc cancels | `tests/test_settings_cache_maintenance.py::test_clear_cancel_path` / `test_clear_yes_path` | test |

## Non-functional requirements

| ID | Requirement | Verification | Status |
|---|---|---|---|
| NF1 | Zero behavioural change for users not opting in | `tests/test_pdf_extras_optional.py` gated by `_extras_absent_only` | test |
| NF2 | Install/uninstall prompts disclose disk size + network impact before any download | `tests/test_extras_cli.py::test_install_dry_run_discloses_disk_and_network` | test |
| NF3 | Install fails cleanly if user declines prompt (no half-installed state) | `tests/test_extras_cli.py::test_install_aborted_when_user_declines` | test |
| NF4 | docling fallback failure (crash, timeout, missing) → pymupdf4llm output kept | implemented: `_try_docling_fallback` returns "" on any exception | done (test pending) |
| NF5 | Per-page extraction: pymupdf4llm <0.25s, docling <0.6s on born-digital A4 (M1 Max) | `tools/pdf_bakeoff/` harness | verif (Phase 0) |
| NF6 | `fnd extras status` reports disk usage within ±10% of actual | implemented: `actual_disk_mb` walks tool venvs + cache dirs | done (verif pending) |
| NF7 | No pymupdf4llm/docling imports at fnd startup when extra is absent | `tests/test_pdf_extras_optional.py::test_optional_extractors_not_imported_eagerly` | test |
| NF8 | Cache lookup adds <100ms on a 300-page payload | `tests/test_extraction_cache.py::test_get_round_trip_under_20ms_for_typical_payload` | test |
| NF9 | Warm-cache reindex is ≥50× faster than cold | HBR Handbook smoke: 44.1s cold → 0.5s warm = 88× | verif |
| NF11 | UI stays responsive during indexing | `asyncio.to_thread` in `_process_one_file`; manual verification on real corpus pending | done (manual verif pending) |
| NF12 | Extras install disclosure includes indexing-time estimate | `tests/test_extras_cli.py::test_install_dry_run_discloses_indexing_time_for_pdf_structure` | test |

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
