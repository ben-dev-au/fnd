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
VISIBLE_FIRST_ABOVE = 7
VISIBLE_FIRST_BELOW = 7
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
FULLMOUNT_CHUNK_BUDGET = 250
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
