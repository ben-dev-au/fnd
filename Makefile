.PHONY: sync test test-fast lint fmt tui snapshot install-hooks batch-close flake-soft harness-real

# Private additions from the dev/ tree, when present. It may set
# BATCH_CLOSE_DONE, a command run once batch-close has passed.
-include dev/tools/local.mk

# Scenarios that build their own fake HOME. The `real_*` three read the
# developer's own documents, so they are opt-in via `harness-real`.
HARNESS_SCENARIOS = tui_renders_chrome open_settings_palette drill_into_indexing \
	escape_returns_to_root update_index_runs liveness_probe_during_index \
	cancel_mid_flight cancel_via_esc_dialog warm_cache_hit_rate \
	indexer_status_lines indexer_status_lines_md_only alt_screen_invariant \
	signature_stability

sync:
	uv sync --all-extras --group dev

test:
	uv run python scripts/run_tests.py -q

test-fast:
	uv run python scripts/run_tests.py -q -m "not slow"

lint:
	uv run ruff check .
	uv run pyright

fmt:
	uv run ruff format .
	uv run ruff check --fix .

# Everything a batch of commits must pass before it is called done. One
# command, because four remembered steps went unrun for 23 commits. The dev/
# instruments live in a separate private tree and are skipped when absent.
batch-close:
	uv run ruff format --check .
	uv run ruff check .
	uv run pyright
	uv run python scripts/run_tests.py -q
	@if [ -f dev/tools/workflow_audit_tmux.py ]; then \
		uv run python dev/tools/workflow_audit_tmux.py $(HARNESS_SCENARIOS); \
	else echo "skip: dev/tools/workflow_audit_tmux.py absent"; fi
	@$(BATCH_CLOSE_DONE)

# Detector A: the suite with `_wait_for_screen`'s bound shrunk to 1ms. Produces
# false positives by design, so it is a round-close instrument, not a gate.
flake-soft:
	@if [ -d dev/tools/flake ]; then \
		PYTHONPATH=dev/tools/flake FND_DEGRADE=soft .venv/bin/python -m pytest \
			-q -p no:randomly -p degrade_plugin -m "not slow"; \
	else echo "skip: dev/tools/flake absent"; fi

# Reads the developer's own documents through symlinks into a private index.
harness-real:
	@if [ -f dev/tools/workflow_audit_tmux.py ]; then \
		uv run python dev/tools/workflow_audit_tmux.py \
			real_index_one_collection real_update_all_chain real_update_all_top5; \
	else echo "skip: dev/tools/workflow_audit_tmux.py absent"; fi

tui:
	uv run textual run --dev fnd.tui.app:FNDApp

snapshot:
	uv run pytest --snapshot-update

install-hooks:
	uv run pre-commit install
