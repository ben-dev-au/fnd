"""Search orchestration for the TUI.

``SearchController`` owns the searcher handle, the active query and its
match spec, the result groups, and the search trace; ``run()`` is the
single query entry point. Preview-cache invalidation inside ``run()``
and ``clear_results()`` still reaches through the app while the preview
subsystem awaits extraction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.widgets import Static

from fnd.matching import MatchSpec
from fnd.query import FileGroup, Hit, Searcher
from fnd.rerank import RankingProfile, profile_from_config

if TYPE_CHECKING:
    from fnd.explain import SearchTrace
    from fnd.synonyms import SynonymTable
    from fnd.tui.app import FNDApp

__all__ = ["SearchController"]


class _PrefixingSearcher:
    """Wrap a :class:`Searcher` and AND a fixed filter prefix into every
    query string before it reaches Tantivy.

    Fusion's phrase pass would otherwise wrap the whole query (including
    field-restrictor prefixes like ``kind:md``) in quotes, which the
    Tantivy parser reads as a literal phrase. By keeping the lexical
    part clean and re-attaching the filter prefix at every sub-query
    issue point, both fusion and cascade get correct field-restricted
    behaviour without changing their public signatures.
    """

    def __init__(self, inner: Searcher, *, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix.strip()

    def _wrap(self, query: str) -> str:
        if not self._prefix:
            return query
        return f"({self._prefix}) AND ({query})"

    def _filtered_raw_hits(self, query: str, **kwargs: Any) -> list[Hit]:
        return self._inner._filtered_raw_hits(self._wrap(query), **kwargs)

    def _raw_hits(self, query: str, **kwargs: Any) -> list[Hit]:
        return self._inner._raw_hits(self._wrap(query), **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Forward attribute access to the underlying searcher (e.g.
        # ``_searcher`` for fuzzy_pass's typed-API path, plus public
        # methods callers might still want).
        return getattr(self._inner, name)


class SearchController:
    """Owns search state and the query entry points; one instance lives
    on the app for the session."""

    def __init__(self, app: FNDApp) -> None:
        self._app = app
        self.searcher: Searcher | None = None
        self.current_query: str = ""
        # Cached match-spec for the active query — drives the markdown-
        # widget highlight subclasses, the per-line plain renderer's
        # highlight pass, and the match-aware scrollbar marker map. The
        # spec captures the SAME literal / fuzzy / synonym semantics
        # the cascade uses, so any word the searcher would have hit on
        # gets the user-visible highlight (not just exact-stem hits).
        # Recomputed on every ``_run_query``.
        self.match_spec: MatchSpec = MatchSpec()
        # Distraction-free reading toggle. When ``False`` the renderers
        # see an empty MatchSpec and emit no highlight spans / scrollbar
        # markers, leaving the preview as plain text. The current
        # query stays intact so flipping the toggle back on restores
        # highlights without re-running the search. Bound to ``h`` via
        # the action registry.
        self.highlights_enabled: bool = True
        # Last :multi block's intent line, if any. Disables strong-signal
        # bypass and biases snippet selection (UX-pass-4 §3). None until
        # the user submits a :multi block.
        self.intent: str | None = None
        self.groups: list[FileGroup] = []
        # Most-recent SearchTrace, populated on every _run_query so the
        # :explain overlay (UX-pass-4 §2) can dump it as JSON. None until
        # the first search runs.
        self.latest_trace: SearchTrace | None = None
        # Synonyms for §9c cascade and §9d fusion's ``syn`` sub-query.
        # Bundled curated defaults + the user's optional personal table;
        # missing personal file is fine (defaults still apply).
        from fnd.config import app_data_dir
        from fnd.synonyms import SynonymTable, load_default_synonyms, load_merged_synonyms

        try:
            self.synonyms: SynonymTable = load_merged_synonyms(app_data_dir() / "synonyms.toml")
        except Exception:
            # A bad personal file is already skipped inside the loader; this is
            # a last resort — still keep the bundled defaults, not an empty table.
            try:
                self.synonyms = load_default_synonyms()
            except Exception:
                self.synonyms = SynonymTable()
        # Ranking profile applied at search time. Built from the active
        # collection's ``ranking_profile`` field once the scope exists;
        # default profile (all-zero) is the BM25 identity.
        self.ranking_profile: RankingProfile = RankingProfile()

    def resolve_profile(self) -> RankingProfile:
        """Pick the ranking profile to apply to search results.

        Resolution order:
          1. If a single collection is active and its ``ranking_profile``
             is defined in the config, use that.
          2. Else fall back to the ``default`` ranking profile if defined.
          3. Else neutral (BM25 identity) — return ``RankingProfile()``.
        """
        if self._app._config is None:
            return RankingProfile()
        name = "default"
        if len(self._app._scope.collections) == 1:
            try:
                col = self._app._config.collection(self._app._scope.collections[0])
                name = col.ranking_profile or "default"
            except KeyError:
                name = "default"
        return profile_from_config(self._app._config.ranking_profile(name))

    def run(self, query: str) -> None:
        if self.searcher is None:
            return
        # Re-point the searcher at the latest committed generation so a
        # reindex (in-app or external `fnd reindex`) that landed while the
        # app is open shows up on this query — no restart. Near-free
        # (~0.1 ms) when nothing changed; ignore a vanished index dir.
        import contextlib as _contextlib

        with _contextlib.suppress(FileNotFoundError, RuntimeError, ValueError):
            self.searcher.reload()
        # A new query must always re-render the first result, even when it
        # lands on the same (parent, seq) as the last one — release the
        # in-flight coalescing latch so this query's dispatch isn't
        # mistaken for a redundant same-tick duplicate of the previous.
        self._app._preview.inflight_target = None
        from fnd.filter_dsl import FilterError
        from fnd.query_errors import QueryError
        from fnd.query_plan import QueryPlan

        # One validated plan: bounds, inline [filter] split, proximity. Malformed
        # queries surface a calm inline notice and the search doesn't run.
        try:
            plan = QueryPlan.from_user_text(query)
        except QueryError as e:
            self._show_query_notice(e)
            self.groups = []
            # Drop the last good trace so :explain can't show a stale plan (#61).
            self.latest_trace = None
            self._app._results.refresh()
            return
        self._clear_query_notice()
        lexical = plan.lexical
        metadata_filter = plan.metadata_filter

        self.current_query = query  # save the original (with [...]) for history
        # Build a comprehensive MatchSpec covering literal stems +
        # fuzzy-AUTO variants + synonym expansions, mirroring the
        # cascade's match semantics. Every preview render this query
        # drives reads from this single spec so the highlight rules
        # never drift from the search rules.
        defaults = self._app._config.defaults if self._app._config else None
        self.match_spec = MatchSpec.from_query(
            lexical,
            synonyms=self.synonyms,
            auto_fuzzy=defaults.fuzzy_enabled if defaults else True,
            min_term_chars=defaults.fuzzy_min_term_chars if defaults else 0,
            multicolour=defaults.multicolour_highlights if defaults else True,
        )
        # Phase F: build the filter scaffolding (kind:, mtime:) and
        # multi-collection scope (c:) as a SEPARATE prefix. The lexical
        # part stays clean so the §9d fusion phrase-pass can wrap it
        # in quotes without dragging field qualifiers inside the
        # phrase (which Tantivy would parse as a literal phrase
        # ``kind:md glimmer`` rather than a field-restricted query).
        filter_clauses: list[str] = []
        if self._app._scope.filter_kinds:
            if len(self._app._scope.filter_kinds) == 1:
                filter_clauses.append(f"kind:{self._app._scope.filter_kinds[0]}")
            else:
                filter_clauses.append(f"kind:({' '.join(sorted(self._app._scope.filter_kinds))})")
        if self._app._scope.filter_date and self._app._scope.filter_date != "any":
            filter_clauses.append(f"mtime:{self._app._scope.filter_date}")
        # Collections are a HARD filter, passed as a list straight to the
        # query layer — never a ``c:`` prefix string. The prefix path rides
        # the soft query parser (ranks instead of restricting) and splits
        # collection names on spaces, so a multi-collection scope leaked
        # other collections and dropped spaced names like ``SSD Exam``.
        cols = self._app._scope.collections
        collection_scope: str | list[str] | None = cols or None
        filter_prefix = " ".join(filter_clauses)
        cfg_defaults = self._app._config.defaults if self._app._config else None
        sections_cap = cfg_defaults.sections_per_file_max if cfg_defaults else 200
        sections_threshold = cfg_defaults.sections_score_threshold if cfg_defaults else 0.5
        try:
            self.groups = self._search_layered(
                lexical=lexical,
                filter_prefix=filter_prefix,
                limit=50,
                sections_per_file=sections_cap,
                sections_score_threshold=sections_threshold,
                collection=collection_scope,
                metadata_filter=metadata_filter,
                active_sources=list(self._app._scope.active_sources) or None,
            )
        except (QueryError, FilterError) as e:
            self._show_query_notice(e)
            self.groups = []
            # Drop the last good trace so :explain can't show a stale plan (#61).
            self.latest_trace = None
            self._app._results.refresh()
            return
        self._clear_query_notice()
        # New query → invalidate BOTH caches:
        # * _chunk_cache (decoded chunk data; rebuilt by next decode)
        # * _preview_cache (mounted widgets; their highlights were baked
        #   from the previous query, so they're stale even if the file
        #   shows up in the new results)
        # The cache invalidation also drops the rendered widgets from
        # the DOM so the next preview load starts from a clean slate.
        import contextlib

        # Invalidate any in-flight mount before clearing: its deferred finally
        # must drop its (now-stale) container instead of re-caching it back into
        # the cache we clear just below.
        self._app._preview.bump_reset_generation()
        # Cancel any debounced load from the prior result set so this reset block
        # is self-contained. (_results.refresh() below also cancels it, but keep
        # the invalidation explicit here alongside the mount/cache teardown,
        # mirroring clear_results(), rather than relying on that later side
        # effect.)
        self._app._preview.cancel_pending_load()
        self._app._preview.chunk_cache.clear()
        # Bundles bake highlight spans from the previous query, so they
        # go stale at the same moment the chunk cache does.
        self._app._preview.prebuilt_cache.clear()
        self._app._preview.cancel_mount_task()
        self._app._lazy.cancel()
        evicted = self._app._preview.preview_cache.clear()
        for old in evicted:
            with contextlib.suppress(Exception):
                old.remove()
        # Also drop the currently-active container if any (it was
        # already evicted above if it was in cache; otherwise it's a
        # small file that wasn't cached and we still need to clear).
        if self._app._preview.active is not None and self._app._preview.active.parent is not None:
            with contextlib.suppress(Exception):
                self._app._preview.active.remove()
        self._app._preview.active = None
        # Highlights baked into every cached doc are stale on query change.
        self._app._flat.cache.clear()
        self._app._flat.reset()
        self._app._preview.chunk_widgets = {}
        self._app._preview.match_targets = {}
        self._app._preview.parent_id = None
        self._app._preview.hide_progress_bar()
        # Drop stale match-nav stops + the k/N indicator; the next preview mount
        # rebuilds them (refresh_match_scrollbar).
        self._app._match_nav.rebuild()
        self._app._results.refresh()
        # Defer prefetch start so the top result's user-side render gets the
        # main thread to itself for the first ~half-second. Without the
        # delay, 10 parallel prefetch mount tasks starve the auto-load.
        self._app.set_timer(0.5, self._app._prefetch.prefetch_top_results, name="prefetch-defer")

    def _show_query_notice(self, err: Exception) -> None:
        """Render a calm, practical line below the query bar for a malformed
        query — message plus an actionable hint where we have one."""
        from fnd.filter_dsl import FilterError
        from fnd.query_errors import QuerySyntaxError

        if isinstance(err, FilterError):
            text = f"filter: {err.message} (col {err.column})"
        elif isinstance(err, QuerySyntaxError):
            text = err.message if not err.hint else f"{err.message} — {err.hint}"
        else:
            text = str(err)
        try:
            notice = self._app.query_one("#query_notice", Static)
        except Exception:
            return
        notice.update(text)
        notice.display = True

    def _clear_query_notice(self) -> None:
        try:
            notice = self._app.query_one("#query_notice", Static)
        except Exception:
            return
        if notice.display:
            notice.update("")
            notice.display = False

    def _search_layered(
        self,
        *,
        lexical: str,
        filter_prefix: str,
        limit: int,
        sections_per_file: int,
        sections_score_threshold: float = 0.0,
        collection: str | list[str] | None,
        metadata_filter: str | None,
        active_sources: list[str] | None,
    ) -> list[FileGroup]:
        """Master plan §9c + §9d wiring + UX-pass-4 §1 strong-signal regime.

        Delegates the regime decision to :func:`fnd.layered.search_layered`
        so the TUI and CLI share one entry point. ``filter_prefix`` is
        applied via :class:`_PrefixingSearcher` so fusion + cascade +
        the regime probe all see the same effective query without any
        signature changes.
        """
        if self.searcher is None or not lexical.strip():
            self.latest_trace = None
            return []
        from fnd.layered import search_layered

        searcher = (
            _PrefixingSearcher(self.searcher, prefix=filter_prefix)
            if filter_prefix
            else self.searcher
        )
        defaults = self._app._config.defaults if self._app._config else None
        groups, trace = search_layered(
            searcher,  # type: ignore[arg-type]
            query=lexical,
            limit=limit,
            sections_per_file=sections_per_file,
            sections_score_threshold=sections_score_threshold,
            collection=collection,
            synonyms=self.synonyms,
            metadata_filter=metadata_filter,
            active_sources=active_sources,
            intent=self.intent,
            profile=self.ranking_profile,
            auto_fuzzy_enabled=defaults.fuzzy_enabled if defaults else True,
            min_term_chars=defaults.fuzzy_min_term_chars if defaults else 0,
            with_trace=True,
        )
        self.latest_trace = trace
        return groups

    def query_signature(self) -> str:
        """Stable signature for the current query — match-bearing
        widgets are baked with this query's highlights, so the cache
        must invalidate when it changes. Includes intent because intent
        biases snippet selection (UX-pass-4 §3), and the highlight
        toggle state because the rendered spans differ on/off: without
        it, toggling highlights re-uses the opposite-state cached
        container for the same file + query and the toggle has no
        visible effect."""
        return f"{self.current_query}|{self.intent or ''}|hl={int(self.highlights_enabled)}"

    def toggle_highlights(self) -> None:
        """Flip the search-highlight overlay on/off without re-running
        the query. Re-renders the currently-shown preview file from
        scratch so the new state takes effect immediately on whatever
        the user is reading."""
        self.highlights_enabled = not self.highlights_enabled
        self._app.notify(
            "Highlights " + ("on" if self.highlights_enabled else "off"),
            timeout=1.5,
        )
        self._app._preview.rerender_current()

    def toggle_fuzzy(self) -> None:
        """Flip ``defaults.fuzzy_enabled`` in the config TOML and re-run
        the current search so the new state is visible immediately.
        Per-term ``~N`` modifiers still trigger fuzzy expansion when
        the toggle is off — only the auto-fuzzy pass is gated."""
        from fnd.config import default_config_path, write_setting

        current = self._app._config.defaults.fuzzy_enabled if self._app._config else True
        new_value = not current
        try:
            self._app._config = write_setting(
                config_path=default_config_path(),
                dotted_path="defaults.fuzzy_enabled",
                value=new_value,
            )
        except Exception as e:
            self._app.notify(f"Couldn't toggle fuzzy: {e}", severity="error", timeout=3)
            return
        self._app.notify(
            "Fuzzy " + ("on" if new_value else "off"),
            timeout=1.5,
        )
        if self.current_query.strip():
            self.run(self.current_query)

    def clear_results(self) -> None:
        """Drop the current result set and preview without re-running.

        Used when the user changes scope (toggles a collection) and the
        existing results are about to go stale — but we don't want to
        steal focus or thrash through a fresh search until the user
        explicitly asks for one. Mirrors the cache invalidation in
        ``_run_query`` minus the actual search call and the
        ``tree.focus()`` step inside ``_refresh_results_tree``.
        """
        import contextlib

        self.groups = []
        # See run(): drop any in-flight mount's stale container rather than let
        # its finally re-cache it after this clear.
        self._app._preview.bump_reset_generation()
        self._app._preview.chunk_cache.clear()
        self._app._preview.prebuilt_cache.clear()
        self._app._preview.cancel_mount_task()
        self._app._lazy.cancel()
        self._app._preview.cancel_pending_load()
        evicted = self._app._preview.preview_cache.clear()
        for old in evicted:
            with contextlib.suppress(Exception):
                old.remove()
        if self._app._preview.active is not None and self._app._preview.active.parent is not None:
            with contextlib.suppress(Exception):
                self._app._preview.active.remove()
        self._app._preview.active = None
        self._app._flat.cache.clear()
        self._app._flat.reset()
        self._app._preview.chunk_widgets = {}
        self._app._preview.match_targets = {}
        self._app._preview.parent_id = None
        self._app._preview.hide_progress_bar()
        # Rebuild the results tree (now empty). The empty-groups branch
        # in ``_refresh_results_tree`` skips ``tree.focus()``, so focus
        # stays in the panel the user is currently driving.
        self._app._results.refresh()
