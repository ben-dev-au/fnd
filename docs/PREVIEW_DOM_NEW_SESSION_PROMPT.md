# Prompt — fresh session for the preview-pane DOM rework

> Note: env vars referenced below were renamed `FND_*` → `_FND_*` on 2026-05-19
> (private-knob convention). Older snapshots in this doc may show the old names.


Copy this into a new Claude Code session at the repo root (or any
worktree pointing at this project). It's self-contained; the new
session does not need access to the prior conversation.

---

## Prompt body (copy from here)

I want to start implementing the staged preview-pane DOM rework
planned in `docs/PREVIEW_DOM_PLAN.md`. Read that file first — it is
the source of truth and consolidates the prior investigation. Read
`preview-dom-analysis.md` at the repo root too; it is the primary
code-anchored analysis the plan is built on.

**Before doing anything else, please:**

1. Confirm you can locate both documents.
2. Re-state the five-stage plan in your own words so I know we are
   aligned. Especially confirm the constraints (functional 1–11 and
   perf A–D) you must preserve.
3. Identify which stages are already partly done from the
   investigation branch (see the "Current state" section of the plan
   and the commits on tag
   `investigation/preview-perf-2026-05-14-handoff`).
4. Propose the working branch name (e.g. `feat/preview-dom-rework`)
   and confirm we should branch off `main`, not off the investigation
   branch.

**Then start Stage 0** — the ~1-hour synthetic verification of the
Textual-screens compositor claim. Spec:

- Path: `tests/perf/spike_screen_compositor.py`.
- Two Screens A and B, each containing ~200 `Static` widgets.
- Push B; A suspends.
- Instrument so you can count the widgets the compositor actually
  walks per refresh tick — either by wrapping
  `app.screen._compositor.render`, by overriding `render` on the
  Static widgets and incrementing a counter, or by reading
  `app.screen._compositor.full_map` length (whichever proves the
  same thing reliably).
- Report the counts on screen B only vs A+B before deciding.

**Decision gate.** Report the measurement back to me and wait for
sign-off before proceeding to Stage 1 (the `RenderedDocument`
refactor). Do not start refactoring code until I confirm Stage 0's
result is what we expected.

**Hard constraints that must hold across every stage:**

Functional:

1. Scroll to chunk + matched line.
2. Per-character match highlights (literal / fuzzy / focused-band).
3. Visible chunk-boundary separator.
4. Match-position tick markers on the scrollbar at the true line
   position. **Note:** structural path currently uses chunk-uniform
   positions; converge to line-precise via the structural map in
   Stage 1.
5. Sidebar's per-hit metadata (page N of M) — renderer-independent.
6. Cross-file LRU cache — revisits are instant.
7. Cursor-following prefetch buffer — next N files pre-decoded.
8. Multi-line text selection and clipboard copy.
9. Markdown rendering: headings with per-level CSS, paragraphs,
   lists, blockquotes, inline code/emphasis, fenced code blocks with
   `rich.syntax.Syntax`, **tables as DataTable so wide tables can
   scroll**, link fallbacks.
10. Live query re-runs update highlights without re-decoding or
    re-rendering — only the match spans change.
11. ~1000-page PDF / DOCX / PPTX and ~100k-line plain text both
    behave acceptably.

Performance:

- A. Steady-state cache-hit click latency <100 ms perceived.
- B. `pilot.pause()` median ≤25 ms, max ≤50 ms.
- C. LRU cache must not let DOM widget count scale with cache size.
- D. Pre-mount must not block keystroke handling.

**Existing measurement harnesses ready to use** (all in `tests/perf/`):

- `bench_user_symptoms.py` — per-click wall-clock to title-update /
  widget-mounted / first-match-resolved / widget-visible /
  do-scroll-completion. Catches the symptoms the user reports.
- `bench_input_lag.py` — pilot.pause distributions by phase + DOM
  widget count snapshot.
- `bench_prefetch_window.py` — prefetch window vs cursor position
  diagnostic.
- `auto_test.py` — cold-path elapsed and scroll-count parsing.

Diag log: `/tmp/fnd-preview-diag.log` when `FND_PREVIEW_DIAG=1`,
timestamped with monotonic seconds.

**What NOT to do** (all three external analyses agree these are dead
ends):

- Patching `display: none` to skip the compositor.
- Caching `Console.render` bytes and blitting directly.
- A full SumTree structural index (overkill — sorted-list + set is
  enough).
- Subtree-detach hacks via private Textual internals.
- Reverting the investigation branch's commits — the plan's
  Stage 0a was a forward-only restore; history is preserved at tag
  `investigation/preview-perf-2026-05-14-handoff`.

**Workflow expectations** (also captured in user memory):

- Build/extend the harness BEFORE writing fixes; data drives
  decisions ([[measure-then-implement]]).
- Propose any architectural change (widget-type swap, default flip,
  component removal) BEFORE editing
  ([[propose-before-arch-changes]]).
- Terse, single-line, load-bearing comments only — multi-paragraph
  block comments read as AI tells ([[comment-density]]).
- No AI traces in commits / code / docs / paths
  ([[no-ai-traces]]).

**First three things I want from you:**

1. Confirmation you have read and understood
   `docs/PREVIEW_DOM_PLAN.md` and `preview-dom-analysis.md`.
2. Your re-statement of the five-stage plan and the constraints.
3. A specific implementation plan for the Stage 0 synthetic spike
   (file path, instrumentation approach, what success looks like).

Then we proceed.
