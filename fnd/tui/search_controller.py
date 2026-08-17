"""Search orchestration for the TUI.

``SearchController`` owns the searcher handle, the active query and its
match spec, the result groups, and the search trace; ``run()`` is the
single query entry point. Preview-cache invalidation inside ``run()``
and ``clear_results()`` still reaches through the app while the preview
subsystem awaits extraction.

``run()`` is split three ways so the blocking part can leave the event
loop:

* **prepare** — on the loop. Parsing, validation and spec building are
  cheap, and a malformed query has to raise its notice immediately.
  Produces a frozen :class:`_SearchRequest`.
* **execute** — on a worker thread. Only the actual search. It reads the
  request and the searcher and writes nothing, which is what makes a
  superseded search harmless rather than a race.
* **commit** — back on the loop. Cache invalidation, DOM teardown and the
  results rebuild, in the order they have always run, and skipped
  entirely if a newer query has been issued since.

Textual cancels a superseded thread worker but cannot interrupt it, so
the generation guard at the top of ``_commit`` is the actual mechanism
that keeps a stale result out, not a belt-and-braces extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from textual.widgets import Static

from fnd.matching import MatchSpec
from fnd.query import FileGroup, Hit, Searcher
from fnd.rerank import RankingProfile, profile_from_config
from fnd.tui.progress.facility import ProgressSession
from fnd.tui.progress.operations import SEARCH

if TYPE_CHECKING:
    from textual.timer import Timer

    from fnd.explain import SearchTrace
    from fnd.synonyms import SynonymTable
    from fnd.tag_query import TagFilter
    from fnd.tui.app import FNDApp

__all__ = ["SearchController"]


@dataclass(frozen=True, slots=True)
class _SearchRequest:
    """Everything the worker needs, frozen at prepare time.

    Deliberately holds no reference to the app or the controller: the
    thread must not be able to observe or mutate state that the loop is
    concurrently changing.
    """

    generation: int
    query: str
    lexical: str
    filter_prefix: str
    metadata_filter: str | None
    collection: str | list[str] | None
    active_sources: list[str] | None
    tag_filter: TagFilter | None
    sections_per_file: int
    sections_score_threshold: float
    auto_fuzzy_enabled: bool
    min_term_chars: int
    match_spec: MatchSpec
    evidence_spec: MatchSpec
    profile: RankingProfile
    intent: str | None


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
        # The painting spec minus AUTO-fuzzy — see _run_query. Answers "is the
        # term the user typed visible here?", which auto-fuzzy near-misses would
        # otherwise answer yes to.
        self.evidence_spec: MatchSpec = MatchSpec()
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
        # Handle for the deferred prefetch timer so each new search cancels the
        # previous one instead of stacking. A burst of searches (e.g. toggling
        # several file-type filters) would otherwise queue a prefetch-of-10 per
        # search — dozens of background preview mounts congesting the event loop
        # and making every subsequent nav feel laggy. Only the latest search's
        # top results are worth prefetching, so keep just the last timer alive.
        self._prefetch_timer: Timer | None = None
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
        # Monotonic query counter. ``_prepare`` bumps it; ``_commit`` refuses
        # any result that does not carry the current value. This is the whole
        # of the staleness defence — a superseded thread worker still runs to
        # completion and still arrives, it just finds its generation gone.
        self._generation: int = 0
        self._committed_generation: int = 0

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
        """Issue a query. Returns immediately; the search runs off the loop."""
        request = self._prepare(query)
        if request is None:
            return
        session = self._app._progress.begin(SEARCH, sampler=self._sample)
        self._app.run_worker(
            lambda: self._execute_and_commit(request, session),
            thread=True,
            exclusive=True,
            group="search",
        )

    @property
    def idle(self) -> bool:
        """True when every issued query has been committed or discarded.

        The signal tests should wait on — never a fixed number of pauses.
        """
        return self._committed_generation >= self._generation

    def _sample(self, session: ProgressSession) -> bool:
        """Keep the line up while this query is still the current one."""
        if self.idle:
            return False
        session.enter("query")
        return True

    # ── prepare (event loop) ─────────────────────────────────────

    def _prepare(self, query: str) -> _SearchRequest | None:
        """Parse and validate on the loop, so a malformed query raises its
        notice immediately rather than a thread-hop later. Returns None when
        there is nothing to execute."""
        if self.searcher is None:
            return None
        was_idle = self.idle
        # Claim the generation FIRST, before anything that can fail. _fail()
        # marks the current generation committed, so allocating it after the
        # parse meant a malformed query marked an IN-FLIGHT search's
        # generation as committed — that worker then passed the guard in
        # _commit, restored its stale results and wiped the error notice the
        # user had just been shown.
        self._generation += 1
        generation = self._generation

        # Re-point the searcher at the latest committed generation so a
        # reindex (in-app or external `fnd reindex`) that landed while the
        # app is open shows up on this query — no restart. Near-free
        # (~0.1 ms) when nothing changed; ignore a vanished index dir.
        #
        # Only while nothing is executing. reload() reassigns the Searcher's
        # inner snapshot, and a worker mid-search reads that attribute more
        # than once (fusion issues several sub-queries), so reloading under it
        # could serve one search from two index generations. Skipping is free:
        # the next idle query picks the new generation up.
        import contextlib as _contextlib

        if was_idle:
            with _contextlib.suppress(FileNotFoundError, RuntimeError, ValueError):
                self.searcher.reload()

        from fnd.query_errors import QueryError
        from fnd.query_plan import QueryPlan

        # One validated plan: bounds, inline [filter] split, proximity. Malformed
        # queries surface a calm inline notice and the search doesn't run.
        try:
            plan = QueryPlan.from_user_text(query)
        except QueryError as e:
            self._fail(e)
            return None
        self._clear_query_notice()
        lexical = plan.lexical

        defaults = self._app._config.defaults if self._app._config else None
        # Build a comprehensive MatchSpec covering literal stems +
        # fuzzy-AUTO variants + synonym expansions, mirroring the
        # cascade's match semantics. Every preview render this query
        # drives reads from this single spec so the highlight rules
        # never drift from the search rules.
        match_spec = MatchSpec.from_query(
            lexical,
            synonyms=self.synonyms,
            auto_fuzzy=defaults.fuzzy_enabled if defaults else True,
            min_term_chars=defaults.fuzzy_min_term_chars if defaults else 0,
            multicolour=defaults.multicolour_highlights if defaults else True,
        )
        # The same spec minus AUTO-fuzzy, used to answer "can the user see what
        # they actually searched for here?" (fnd.tui.match_evidence).
        #
        # Painting is deliberately generous: auto-fuzzy lights up near-misses so
        # a typo still shows something. But near-misses are not evidence — a
        # query for "test" paints "best" and "rest" at distance 1, so judging
        # visibility on the painting spec would report every such chunk as fine
        # while the user stares at a paragraph with none of their term in it.
        # An explicit ``term~N`` survives here: the user asked for fuzzy, so a
        # fuzzy hit IS what they searched for.
        evidence_spec = MatchSpec.from_query(
            lexical,
            synonyms=self.synonyms,
            auto_fuzzy=False,
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
        if self._app._scope.filter_created and self._app._scope.filter_created != "any":
            filter_clauses.append(f"created:{self._app._scope.filter_created}")

        # Tags never ride the prefix string — see fnd/tag_query.py. They are
        # typed state, passed down beside the collection scope.
        from fnd.tag_query import TagFilter

        scope = self._app._scope
        tag_filter: TagFilter | None = None
        if any(scope.tag_include.values()) or any(scope.tag_exclude.values()):
            tag_filter = TagFilter(
                include={k: frozenset(v) for k, v in scope.tag_include.items() if v},
                exclude={k: frozenset(v) for k, v in scope.tag_exclude.items() if v},
                match_all=scope.tag_match_all,
            )
        # Collections are a HARD filter, passed as a list straight to the
        # query layer — never a ``c:`` prefix string. The prefix path rides
        # the soft query parser (ranks instead of restricting) and splits
        # collection names on spaces, so a multi-collection scope leaked
        # other collections and dropped spaced names like ``SSD Exam``.
        cols = self._app._scope.collections
        cfg_defaults = self._app._config.defaults if self._app._config else None

        try:
            profile = self.resolve_profile()
        except Exception:
            profile = self.ranking_profile

        return _SearchRequest(
            generation=generation,
            query=query,
            lexical=lexical,
            filter_prefix=" ".join(filter_clauses),
            metadata_filter=plan.metadata_filter,
            # Copied, not referenced: the scope panel mutates these lists on
            # the event loop while the worker is reading them.
            collection=list(cols) if cols else None,
            active_sources=list(self._app._scope.active_sources) or None,
            tag_filter=tag_filter,
            sections_per_file=cfg_defaults.sections_per_file_max if cfg_defaults else 200,
            sections_score_threshold=(
                cfg_defaults.sections_score_threshold if cfg_defaults else 0.5
            ),
            auto_fuzzy_enabled=defaults.fuzzy_enabled if defaults else True,
            min_term_chars=defaults.fuzzy_min_term_chars if defaults else 0,
            match_spec=match_spec,
            evidence_spec=evidence_spec,
            profile=profile,
            intent=self.intent,
        )

    def _fail(self, err: Exception) -> None:
        """A query that cannot run: show the notice and drop the stale trace
        so :explain can't display a plan for a query that never executed."""
        self._show_query_notice(err)
        self.groups = []
        self.latest_trace = None
        self._committed_generation = self._generation
        self._app._results.refresh()

    # ── execute (worker thread) ──────────────────────────────────

    def _execute_and_commit(self, request: _SearchRequest, session: ProgressSession) -> None:
        """Worker body. Marshals every outcome back to the loop — including
        failures, so a raising search can't leave the line up forever."""
        from fnd.filter_dsl import FilterError
        from fnd.query_errors import QueryError

        try:
            groups, trace = self._execute(request)
        except (QueryError, FilterError) as e:
            self._marshal(self._commit_failure, request, e, session)
            return
        except Exception as e:  # never strand the UI on an unexpected search bug
            self._marshal(self._commit_failure, request, e, session)
            return
        self._marshal(self._commit, request, groups, trace, session)

    def _marshal(self, fn: Any, *args: Any) -> None:
        """Hop back to the event loop with the outcome.

        A search can still be in flight when the app shuts down — the user
        quits mid-query, or a test leaves its ``run_test`` block — and the
        commit then has no DOM left to write to. That is not an error, and it
        must not surface as a failed worker. A failure while the app IS still
        running is a real bug, so it still propagates.
        """
        try:
            self._app.call_from_thread(fn, *args)
        except Exception:
            if getattr(self._app, "is_running", False):
                raise

    def _execute(self, request: _SearchRequest) -> tuple[list[FileGroup], SearchTrace | None]:
        """The blocking part, off the loop. Reads the request and the searcher;
        writes nothing."""
        if self.searcher is None or not request.lexical.strip():
            return [], None
        from fnd.layered import search_layered

        searcher = (
            _PrefixingSearcher(self.searcher, prefix=request.filter_prefix)
            if request.filter_prefix
            else self.searcher
        )
        groups, trace = search_layered(
            searcher,  # type: ignore[arg-type]
            query=request.lexical,
            limit=50,
            sections_per_file=request.sections_per_file,
            sections_score_threshold=request.sections_score_threshold,
            collection=request.collection,
            synonyms=self.synonyms,
            metadata_filter=request.metadata_filter,
            active_sources=request.active_sources,
            tag_filter=request.tag_filter,
            intent=request.intent,
            profile=request.profile,
            auto_fuzzy_enabled=request.auto_fuzzy_enabled,
            min_term_chars=request.min_term_chars,
            with_trace=True,
        )
        return groups, trace

    # ── commit (event loop) ──────────────────────────────────────

    def _commit_failure(
        self, request: _SearchRequest, err: Exception, session: ProgressSession
    ) -> None:
        if request.generation != self._generation:
            session.close()
            return
        self._fail(err)
        session.close()

    def _commit(
        self,
        request: _SearchRequest,
        groups: list[FileGroup],
        trace: SearchTrace | None,
        session: ProgressSession,
    ) -> None:
        """Land the results. Everything below has always run AFTER the search
        returned; keeping it whole here preserves that order."""
        # Superseded. Textual cancels the worker but cannot interrupt it, so a
        # stale search always runs to completion and arrives here — this guard
        # is what keeps it from touching anything.
        if request.generation != self._generation:
            session.close()
            return
        self._committed_generation = request.generation
        # Everything below runs on the loop and is real, measurable work —
        # cache teardown, the results tree rebuild, the filters aggregation.
        # The plan names it, so enter it: leaving the session in "query" for
        # the whole operation capped the line at that phase's share and meant
        # calibration never saw a "results" duration, so its weight stayed at
        # the seed forever.
        session.enter("results")

        self._clear_query_notice()
        self.latest_trace = trace
        self.groups = groups
        self.match_spec = request.match_spec
        self.evidence_spec = request.evidence_spec
        self.current_query = request.query  # the original (with [...]) for history

        # A new query must always re-render the first result, even when it
        # lands on the same (parent, seq) as the last one — release the
        # in-flight coalescing latch so this query's dispatch isn't
        # mistaken for a redundant same-tick duplicate of the previous.
        self._app._preview.inflight_target = None

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
        # Tag counts describe the current result set, so they go stale the
        # moment a query changes. Cheap (single aggregation, ~5 ms on a
        # 72k-doc index) and the pane restores its own cursor.
        self._app._scope.refresh_filters_panel()
        # Defer prefetch start so the top result's user-side render gets the
        # main thread to itself for the first ~half-second. Without the
        # delay, 10 parallel prefetch mount tasks starve the auto-load.
        # Cancel the prior deferred prefetch first: a burst of searches must not
        # stack N prefetch-of-10 batches (only the latest result set matters).
        if self._prefetch_timer is not None:
            self._prefetch_timer.stop()
        self._prefetch_timer = self._app.set_timer(
            0.5, self._app._prefetch.prefetch_top_results, name="prefetch-defer"
        )
        session.close()

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
