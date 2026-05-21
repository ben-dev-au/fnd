# End-to-end workflow tests

These tests drive each settings workflow through an actual mounted
`FNDApp`, simulating real keypresses and asserting on real state
transitions. They catch behavioural regressions that SVG-snapshot
audits miss.

## Running

```sh
uv run pytest tests/workflows/         # all workflows
uv run pytest tests/workflows/test_update_all.py -v
```

## Quick CLI audit

`tools/workflow_audit.py` runs a subset of the workflows as a
fast PASS/FAIL summary, useful between code changes:

```sh
uv run python tools/workflow_audit.py
```

## Workflow coverage

| Workflow | File | What's asserted |
|---|---|---|
| Update all collections | `test_update_all.py` | Chain advances through every collection; Cancel keeps state clean; empty queue is a no-op. |
| Install pdf-structure | `test_install_pdf_structure.py` | Commands target fnd's actual Python; pre-install repair sweeps orphan dist-info dirs; confirm screen mounts; modal handles `failed` events without crashing. |
| Uninstall pdf-structure | `test_uninstall_pdf_structure.py` | Commands target fnd's actual Python; `--dry-run` shows the full plan; namespace-husk packages read as not installed; PATH shadows ignored for uv-tool packages. |
| Cache actions | `test_cache_actions.py` | Prune on empty cache no-ops; Clear on empty cache no-ops; Clear with entries pushes a destructive confirm. |
| Per-collection Update | `test_per_collection_update.py` | Single-collection title doesn't show `(X of Y)`; unknown collection fails gracefully. |
| First-reindex warning | `test_first_reindex_warning.py` | Skipped when collection has no PDFs; Start marks seen; Cancel doesn't mark seen. |
| Delete collection | `test_delete_collection.py` | Confirm screen mounts; Cancel preserves the collection in the config. |
| Toggles | `test_toggles.py` | Auto-resume persists; cache-at-index-time persists; off-state propagates to `_skip_structure_extraction`. |

## Adding a new workflow

1. Create `test_<verb>_<noun>.py` in this dir.
2. Use the shared fixtures from `conftest.py` (`app_factory`,
   `cfg_one`, `cfg_three`, `mini_corpus`, `built_index`, `wait_until`).
3. Write the test in three sections:
   - Set up the app + workflow precondition.
   - Drive the UI via `pilot.press(...)` and `pilot.pause()`.
   - Assert the end state (screen stack, app attributes,
     side-effects on disk).
4. If the workflow involves a chain or async task, use `wait_until`
   from `conftest.py` to poll for completion rather than fixed
   `pilot.pause()` counts.
5. Add the workflow to `tools/workflow_audit.py`'s `WORKFLOWS` dict
   too — that script gives a faster PASS/FAIL summary during
   active development.
