"""Mount-window and cache tunables for the preview subsystem.

Read at call time by the preview components so a test (or future
settings surface) can override a value on this module and see it take
effect without re-importing consumers.
"""

# Preview widget cache. Repeat visits to a previously-loaded file
# should be instant — keep the mounted widget tree alive in a per-file
# Container; switching files is then a single class-toggle. LRU-bounded.
# Option A: only the active file stays mounted. Cached inactive containers
# stayed in the pane and inflated every mount/settle (measured net-negative:
# a "hit" rebuilt anyway since is_complete is never true in the windowed
# model, while taxing the active path). 1 = no stale DOM behind the active file.
PREVIEW_CACHE_MAX_FILES = 1
PREVIEW_CACHE_MIN_CHUNKS = 1
# Visible-first mount window — chunks are decoded already, mounting
# focused ± these counts synchronously gives the user instant viewport
# feedback before the background fill starts.
#
# ``VISIBLE_FIRST_ABOVE`` is a CAP, not a count — see
# ``PreviewPresenter.above_window_start``. The reveal cannot happen until every
# chunk above the focus has built (they decide where the match lands, see
# _finalize_via_lock_body), so each one is paid for on the critical path, while
# the ones below are not. Mounting a fixed number above therefore overpays
# whenever the chunks are tall.
#
# How tall a chunk is varies enormously by format: a PDF chunk is a PAGE (30-60
# rows), a markdown chunk is one heading's section (often 2-3 rows). Seven pages
# is several screens of content nobody asked for; seven short sections is less
# than the context margin. So the window is measured in ROWS and this only bounds
# it. Measured on a 1018-chunk PDF (3x40 navigations per setting), the row-based
# window against a flat 7: first paint 1796 -> 1493ms, the finalize's build wait
# 1457 -> 848ms, the reconcile-to-scroll gap 731 -> 517ms.
# Delay before warming extends a captured run to the rest of the file. Long
# enough that the navigation the user just made has fully landed — warming is
# for the jump AFTER this one, so it must never compete with the current paint.
# How long the freeze sweep waits for its container to be revealed before
# giving up (ticks of 50ms). Capturing from a `-pre-reveal` container yields
# blank strips with correct geometry — invisible to every guard, and served
# later as an empty preview.
FREEZE_REVEAL_WAIT_TICKS = 40

# How long the freeze sweep may hold the event loop before yielding, in seconds.
# The sweep swaps every out-of-window chunk's widget tree for its capture, and it
# used to do the whole file in ONE synchronous loop — so the cold-to-frozen
# transition was a hard stall of however long that took. Measured on synthetic
# six-line chunks: 221ms at 57 mounted, 452ms at 147, 559ms at 237, and real
# chunks (PDF pages, rich markdown) cost 46-384ms EACH rather than ~2ms.
#
# Sliced by TIME rather than by a chunk count because per-chunk cost varies by
# two orders of magnitude across formats, so any fixed count is either a stall
# on one file or needless yielding on another. One frame's worth keeps the swap
# invisible without meaningfully lengthening it.
FREEZE_SLICE_SECONDS = 0.016

# How long to wait after a frozen chunk reports a stale width before re-cutting
# its strips. A window drag emits a resize per column and every mounted frozen
# chunk reports independently, so this coalesces a whole gesture into one repair
# pass — and a drag that returns to its original width costs nothing at all.
STALE_STRIP_REPAIR_DELAY = 0.25

# How many repair passes may chain before giving up. A pass abandons whenever the
# width moves again mid-drag, and reports arriving during a pass are dropped, so
# it must be able to re-arm itself — but a chunk whose capture keeps failing
# (`freeze` refuses an unlaid DataTable or a nested scroll region) would re-arm
# forever without a bound. Three is enough for a drag that settles; beyond that
# the next navigation rebuilds correctly anyway.
STALE_STRIP_MAX_PASSES = 3

# Debounce before a coverage pass starts, so a held-down arrow key does not
# spawn a plan per keypress — a superseded plan then does no work at all.
PREVIEW_WARM_DELAY = 0.35

# How many NEIGHBOURING files each side of the cursor coverage captures ahead.
# Between-file navigation is the slow case: within a file the target is already
# mounted or served, but arriving at a file with nothing captured pays the full
# build. Only the neighbours' HIT chunks are captured, so the cost per file is
# its hit count rather than its size — a 1000-chunk neighbour with eight hits
# costs eight captures, and the buffer re-plans around the cursor on every move.
COVERAGE_NEIGHBOUR_FILES = 2

# How many files from the TOP of the result list to warm regardless of where the
# cursor is. The cursor window is relative, so on a fresh query it can only warm
# the first file's immediate neighbours — yet the opening moves of a search are
# the most likely to be made. Seeding the head covers them. Beyond it the window
# takes over: a cursor past the seed warms its own neighbours as before, and the
# seeded files cost a store lookup each to skip.
COVERAGE_SEED_FILES = 8

# The structural renderer's size cap lives in fnd.tui.preview_dispatcher as
# MARKDOWN_MAX_CHARS — that module is what every path asks, and it sits below
# this package, so it cannot import from here.

# How long coverage must stand idle after each capture, as a multiple of what
# that capture cost. A capture builds a real markdown widget, and Textual pumps
# its blocks through the SAME event loop the UI runs on — so a capture in flight
# is a frozen UI, and the yield between captures is the only place to give the
# loop back.
#
# Measured without this, sitting still for 12s after opening a file: 154
# captures, median 52ms each, 10.1 SECONDS of the 12 spent inside a capture —
# 84% of the loop, with stalls up to 393ms. Responsive enough to open a file and
# then find that changing focus or stepping to the next match takes a second or
# more to answer.
#
# Measured share of the event loop coverage takes, over a 12s idle window on a
# real corpus:
#
#   ratio   captures   loop time capturing   worst capture
#   none        154     10.1s of 12  (84%)      384ms
#   2.0          72      4.1s of 12  (34%)      209ms
#   4.0          44      2.5s of 12  (21%)      169ms
#   6.0          33      1.9s of 12  (16%)      159ms
#
# 4.0 because the cache is worth nothing if filling it makes the app feel slow,
# and a fifth of the loop still fills 44 chunks in a quiet 12 seconds. Raise it
# for a calmer app, lower it to warm faster.
COVERAGE_IDLE_RATIO = 4.0
# Ceiling on that idle period, so one pathological chunk cannot park coverage.
COVERAGE_IDLE_MAX = 0.5

# How long warming waits for an in-flight landing before giving up its turn
# (ticks of 50ms). Warming must never compete with the scroll the user is
# waiting on — that competition cost 170ms -> 579ms median on a real corpus.
PREVIEW_WARM_YIELD_TICKS = 40

VISIBLE_FIRST_ABOVE = 7
VISIBLE_FIRST_BELOW = 7
# Rows of content to mount above the focus chunk before the reveal, as a
# multiple of the viewport height. One full screen leaves the context margin
# (MATCH_CONTEXT_FRACTION) satisfied with room to spare, and still lets a short
# upward scroll happen before lazy mount has to extend the window.
VISIBLE_FIRST_ABOVE_SCREENS = 1.0
# Background-fill bound, applied beyond the ±VISIBLE_FIRST_* window
# during the initial cold mount. At < VISIBLE_FIRST_* the phase 2a/2b
# loops are no-ops; the scroll-driven lazy mount picks up from the
# visible-window boundary instead. Raise to e.g. 10 for a small static
# buffer before lazy-mount engages; the trade-off is a small cold-mount
# cost per cached file.
BACKGROUND_FILL_RADIUS = 3
# Option C: when the active file is within this many chunks, background-fill it
# completely so internal match-jumps land on an already-mounted chunk (instant).
# Larger files stay windowed (radius above) to protect DOM size / input lag.
#
# This bounds what is MOUNTED, and it is load-bearing beyond latency: the in-file
# match count walks the mounted subtree (``MatchNavigator._count_stops``), so a
# file that stops being filled stops being counted in full. Measured marginal
# cost of a frozen chunk on a fast machine: 37us of arrange, 78us of a far
# scroll, 0.9ms to mount — linear, no cliff, with the knee for "a navigation
# still feels instant" around 1000-2000 mounted chunks.
FULLMOUNT_CHUNK_BUDGET = 250
# How many chunks of a file coverage will CAPTURE ahead, so a jump outside the
# mounted set mounts them instead of building them. Complements the mount budget
# above rather than replacing it: on a large file the fill reaches nothing, and
# this is the only thing standing between a far jump and a rebuild from source.
#
# The cap is on CAPTURES, not on mounted widgets: captures cost memory (44.5 KB
# each, so 500 is ~22 MB against a machine-scaled cache budget) and the build
# time to make them, while costing no arrange time at all. What stays MOUNTED is
# still the contiguous window — see fnd/tui/preview/coverage.py for why coverage
# must not put its scattered set into the DOM.
COVERAGE_CHUNK_BUDGET = 500
# Chunks captured either side of a hit, so a landing has context above and below
# it rather than a bare match with unbuilt neighbours. Deliberately smaller than
# VISIBLE_FIRST_ABOVE/BELOW (7): coverage is ordered nearest-first, so the chunks
# around wherever the user actually is get captured before distant margins do.
# Swept against the real corpus at 3, 5 and 7, twice: 5 wins both times (34%% of
# mounted chunks served and 3 fully-served landings, against 31%%/1 at margin 3
# and 25%%/2 at margin 7). Matching the mount window exactly is WORSE — the extra
# captures per hit cost more files covered than they buy in completeness.
COVERAGE_MARGIN = 5
# Prefetch mounts only the focused chunk per cached file. User-side
# resume expands on click. Keeps prefetch DOM contribution at
# ~1 widget per cached file.
PREFETCH_MOUNT_RADIUS = 0
# Scroll-driven lazy mount. When the user scrolls within this many
# cells of the boundary of the mounted region, the next batch is
# mounted on demand. Lets long files behave like a continuous document
# without forcing the initial mount to cover everything.
LAZY_MOUNT_TRIGGER_MARGIN = 30
LAZY_MOUNT_BATCH = 3
# Scroll-to-match leaves this fraction of the viewport above the match so
# the user sees context before it, rather than pinning it to the top line.
MATCH_CONTEXT_FRACTION = 0.25
# Reveal watchdog: an active container mounts invisible (``-pre-reveal``) and is
# revealed by its finalize task once the layout settles. Under rapid navigation
# that task can be cancelled before it reveals, or hang for seconds awaiting
# above-window chunks a cancelled mount never mounted — leaving the container
# invisible ("preview blank until I select a different result and come back").
# This is the bounded-time backstop: if a container is still ``-pre-reveal`` this
# long after it became active, reveal it regardless. Set well above a normal
# finalize (~150-700ms) so it rarely pre-empts the no-flash scroll, but far below
# the finalize's own 8s internal timeouts so a hang can't show a multi-second
# blank. Re-armed on every navigation; a fast finalize reveals first and disarms.
REVEAL_WATCHDOG_MS = 1500
# Settle-time paint check. The reveal watchdog above fixes exactly one failure
# mode (still ``-pre-reveal``). This one checks the OUTCOME instead: some while
# after a navigation, is the pane actually showing the file the cursor is on?
# Every seam in the pipeline is written to guarantee that, but it has many
# concurrent writers (mount, prefetch, lazy-mount, three reset paths) and this
# subsystem's history is a succession of one-seam-at-a-time strands. This check
# is the backstop that turns any future strand into one extra rebuild rather
# than a pane that stays blank until the user navigates away and back. Set past
# the reveal watchdog so a slow-but-healthy navigation is never pre-empted.
PAINT_CHECK_MS = 2200
# Re-arms allowed while the pipeline is still legitimately working (a monster
# file can mount for a while). Bounded so a genuinely wedged pipeline still
# reaches the single repair rather than deferring forever.
PAINT_CHECK_MAX_REARMS = 6
