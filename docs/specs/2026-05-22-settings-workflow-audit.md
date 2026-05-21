# Settings workflows — user-perspective audit

Every settings action the user can trigger today, with the questions a
real user would ask before doing it and the gaps between expectation
and current behaviour. The goal is for each action's confirm
screen and on-screen copy to pre-empt every reasonable question the
user has — never leave them guessing.

## Methodology

For every action below:

1. **Trigger** — exact menu path / keyboard.
2. **What the user expects** — naive mental model.
3. **What actually happens** — current behaviour, including side-effects.
4. **Questions the user would ask** — anticipate confusion.
5. **Gaps** — what the current confirm / status copy doesn't answer.
6. **Fix** — concrete copy or behaviour change.

This doc is the source of truth for confirm copy. Any future change
to an action's copy or flow updates this doc first.

---

## 1. Install pdf-structure

**Trigger:** Settings → Indexing → `Install pdf-structure [ Install ]`.

**Expects:** Press Enter → see what it'll install + cost → confirm →
watch it install → done.

**Actually happens:**
- Pushes `StructuredPdfConfirmScreen` with three-row disclosure.
- On Yes: `start_extras_install` runs `uv sync --extra pdf-structure`
  then `uv tool install docling-slim[standard]` in sequence.
- Progress modal mounts; backgrounded with Esc.
- On completion: "Restart fnd to apply."

**User questions:**
- What does this give me? (structured rendering)
- How much disk?
- How much CPU on first reindex?
- What about ML weights — do they download in the install or later?
- Can I cancel? (yes — `c`)
- Can I keep using fnd while it installs? (yes — Esc backgrounds)
- After install, do I need to do anything? (yes — restart fnd, then Update index to populate cache)

**Gaps:**
- Current copy says "Auto-resumes if interrupted" but that's about Update
  index, not the install. Confusing — clarify these are two separate things.
- Doesn't say the user needs to restart fnd AND run Update index to see
  structured PDFs.

**Fix:**
- Confirm body:
  - **Outcome**: PDFs gain structured rendering (headings, lists, tables, recovered image-tables).
  - **Cost**: ~900 MB disk + ~400 MB ML weights on first use. First Update index ~2 s per PDF on born-digital text.
  - **Safety**: Indexed flat-text chunks stay searchable. Cancel mid-install with `c`.
- Completion copy: "✓ Installed. Restart fnd, then run Update index to populate structured PDFs."

---

## 2. Uninstall pdf-structure

**Trigger:** Settings → Indexing → `Uninstall pdf-structure [ Uninstall ]`.

**Expects:** "Remove the extra. Free disk. Done." May not realise the
PDF structure cache is a separate concept.

**Actually happens:**
- Confirm screen pushed.
- On Yes: `uv tool uninstall docling-slim` + `uv sync` (drops pip-extra packages).
- Indexed structured chunks remain in the search index until next reindex.
- PDF structure cache stays on disk — uninstall does NOT touch it.

**User questions:**
- How much disk do I get back?
- What happens to my indexed PDFs? (still searchable until reindex)
- What happens to the cache? (stays — needs separate clear)
- Will I still get search results? (yes — flat text from now on; structured chunks remain in index)
- Can I reinstall later? (yes — clean reinstall)
- Do I need to restart? (yes — running process has the packages imported)

**Gaps (this is what the user just hit):**
- "Cost: ~200 MB disk recovered" — disk recovery is a *benefit*, not a cost.
  Mislabelled. **Fixed**: label changed to "Disk freed".
- Cache impact not mentioned. **Fixed**: Safety row spells out "PDF
  structure cache (X) stays. Clear it separately via Settings →
  Indexing → Clear PDF structure cache."
- Completion copy said "Restart fnd to use it" — nothing to use, it
  was uninstalled. **Fixed**: "Restart fnd to apply."

**Fix (current state):**
- Confirm body (uses overridden labels):
  - **What changes**: New PDF extractions revert to flat text.
  - **Disk freed**: ~200 MB (packages).
  - **Preserved**: Indexed structured PDFs keep working until reindex. PDF structure cache (X GB) stays. Clear it separately via Settings → Indexing → Clear PDF structure cache.
- Completion: "✓ Uninstall complete. Restart fnd to apply."
- After completion + Close button: menu re-evaluates `is_extra_installed` (now disk-truthful, not import-cached) so the row flips to "Install pdf-structure". User is told to restart anyway because the *running* process still has the modules imported.

---

## 3. Update index now (per-collection)

**Trigger:** Settings → Collections → ‹name› → `Update index now [ Update ]`.

**Expects:** "Refresh this collection's search index." Probably doesn't
think about extraction or cache.

**Actually happens:**
- Walks the collection's sources via `walk_sources`.
- For each file: deletes existing chunks for that `parent_id`, calls
  `extract(path)`.
- `extract()` consults the PDF structure cache: hit → return cached
  chunks; miss → run extractor + write to cache (if cache_at_index_time
  is on).
- IndexerScreen mounts, shows progress + cache hit/miss counts.

**User questions:**
- How long will it take?
- What's "current file"?
- Why am I seeing cache hits/misses — what's that?
- Can I keep working? (yes, Esc backgrounds)
- Can I cancel? (yes, `c`)
- Will reindex remove deleted files from the index? (yes)

**Gaps:**
- The cache hit/miss counter on the modal lacks explanation. Add
  tooltip on focus or footer hint.
- No estimate of time remaining beyond raw file count.

**Fix:**
- Row description: "Re-scan this collection's sources. New / changed
  files are added; deleted files are removed; unchanged files are
  skipped. Uses the PDF structure cache to skip extraction when
  content hasn't changed."  ← matches current.
- Modal status line: include ETA (Phase F work — calibrated throughput).

---

## 4. Update all collections

**Trigger:** Settings → Collections → `Update all collections [ Update ]`.

**Expects:** "Refresh every collection." May not realise this is
sequential, not parallel.

**Actually happens:**
- Confirm dialog with cost estimate.
- On Yes: triggers the first collection's Update index via
  `_reindex_with_warning_if_needed`.
- **Current limitation**: only the first collection runs today. Chaining
  to the next on completion is Phase F work.

**User questions:**
- How long for all of them?
- What if one fails — do the rest still run?
- Can I cancel just one?

**Gaps:**
- Sequential chaining isn't implemented — the user sees only one
  collection update.
- No aggregate progress display.

**Fix (deferred to Phase F):**
- Implement sequential chain — listen for IndexerScreen `done` event,
  advance to next collection.
- Aggregate progress modal showing `collection 2 of 5: papers (43% — 187/432 files)`.
- Confirm copy stays as-is for now.

---

## 5. Update cache

**Trigger:** Settings → Indexing → `Update cache [ Update ]`.

**Expects:** "Update the cache." Probably doesn't realise this is
*populate cache without indexing*.

**Actually happens (current limitation):**
- Confirm dialog promised, but the actual worker is a stub (notifies
  "wires up in Phase E"). **Bug**: should be wired by now.

**User questions:**
- Why would I do this instead of Update index?
- Will it touch my search index? (no — just the cache)
- How long?

**Gaps:**
- Action not wired through. **TODO**.
- Description is plausible but doesn't make the use case clear: "pre-warm
  cache after install, before a big Update index."

**Fix (TODO before this row is functional):**
- Wire the worker. Walk every PDF in every collection's sources;
  call `extract(path)` (which populates the cache) but skip the
  index write entirely. The current extract() is tightly coupled to
  the indexer's flow — needs a `populate_cache_only(path)` helper.
- Add the use case to the description: "Pre-warm the cache after
  installing pdf-structure, before a long Update index. Doesn't
  touch the search index."

---

## 6. Prune stale entries

**Trigger:** Settings → Indexing → `Prune stale entries [ Prune… ]`.

**Expects:** "Remove old cache entries." May not know what "stale" means.

**Actually happens:**
- Walks the cache dir, finds entries whose extractor signature ≠ current.
- Confirm dialog with count + cost.
- On Yes: deletes stale entries.

**User questions:**
- What's an "extractor signature"?
- Will the affected files still work? (yes — re-extracted on next reindex)
- How long does the prune itself take? (seconds — just file deletes)

**Gaps:**
- "Extractor signature" leaks implementation. User-facing wording:
  "entries from an older version of the structuring pipeline."
- Cost message says "Next Update index re-extracts those files (~N min)"
  — but doesn't say the prune itself is fast. Add: "Pruning is
  instant; the cost is the next Update index re-extracting."

**Fix:**
- Description: "Remove cache entries from older versions of the
  structuring pipeline. Fresh entries stay. Files with removed
  entries get re-extracted on the next Update index."
- Confirm copy: clarify "instant + later cost."

---

## 7. Clear PDF structure cache

**Trigger:** Settings → Indexing → `Clear PDF structure cache [ Clear… ]`.

**Expects:** "Wipe the cache." Should be obvious it's destructive.

**Actually happens:**
- Destructive confirm with red border, ⚠ Cannot be undone.
- On Yes: `shutil.rmtree` on the cache dir.

**User questions:**
- Will I lose searchability? (no — search index unaffected; structured
  chunks stay until next reindex)
- How long to re-extract? (~2 s per PDF × all PDFs)
- Do I also need to reindex? (yes, to repopulate cache; otherwise next
  reindex will re-extract as it goes)

**Gaps:**
- Doesn't make the search-index-stays-intact promise explicit. **Fixed**: Safety row says "Markdown / DOCX / PPTX previews unaffected" — should also say "Search index untouched."

**Fix:**
- Confirm body:
  - **Outcome**: PDFs render as unstructured text until next Update index.
  - **Cost**: ≈ N min for next Update index (M PDFs × ~2 s/PDF).
  - **Safety**: Search index untouched. Other file types (md/docx/pptx) unaffected.

---

## 8. Auto-resume on launch (toggle)

**Trigger:** Settings → Indexing → `Auto-resume on launch ✓ on / ✗ off`.

**Expects:** Toggle that controls resume behaviour.

**Actually happens:**
- Writes `defaults.indexer_auto_resume` to config.
- On next fnd launch, if a state file from an interrupted Update index
  exists, fnd resumes silently in background.

**User questions:**
- What's an "interrupted" Update index? (force-quit, sleep, etc.)
- Will resume show progress? (no — silent background, footer indicator only)
- What happens if I changed sources since the interrupt? (cache hits
  still work; new files get extracted)
- Can I trigger the resume manually? (yes — Update index now)

**Gaps:**
- Description doesn't mention the silent / background nature.

**Fix:**
- Description: "✓ On — interrupted Update index resumes silently in
  the background next time you open fnd. ✗ Off — Update index must be
  triggered manually after a quit."

---

## 9. Update cache at index time (toggle)

**Trigger:** Settings → Indexing → `Update cache at index time ✓ on / ✗ off`.

**Expects:** Some kind of cache control.

**Actually happens:**
- Writes `defaults.cache_at_index_time` to config.
- During Update index, if On: extract() writes to cache on miss.
  If Off: extract() reads cache on hit but on miss runs the flat
  extractor (no structured pipeline, no cache write).

**User questions:**
- Why would I turn this off? (battery saver — fast flat-text refresh)
- Do existing cache entries still get used? (yes — only writes are skipped)
- If I turn it off, will PDFs lose their structure? (only for NEW files; cached files still render structured)

**Gaps:**
- This toggle is novel; users won't intuit when to use it.
- Description mentions "battery / slow machines" but the use case
  needs more clarity.

**Fix:**
- Description: "When ON (default with pdf-structure installed): Update
  index populates the cache for any PDF without an entry. When OFF:
  Update index uses cached entries for previously-seen files but
  skips fresh extraction for new files — fast flat-text refresh,
  useful when you don't have CPU/battery to spare."

---

## 10. Delete collection

**Trigger:** Settings → Collections → ‹name› → `Delete collection [ Delete… ]`.

**Expects:** Destructive: removes collection + its chunks.

**Actually happens:**
- Destructive confirm.
- On Yes: removes from config, deletes its chunks from the index.

**User questions:**
- Are my source files deleted? (no)
- Are other collections affected? (no)
- What about the cache — is it shared? (yes; entries for shared files stay usable for other collections)
- Can I recover? (no — re-add and reindex)

**Gaps:**
- Confirm body doesn't reassure about source files (the user's actual
  data). Add: "Source files on disk are untouched."

**Fix:**
- Confirm body:
  - **Outcome**: Collection ‹name› removed from config; its chunks dropped from the search index.
  - **Cost**: Cannot be reversed — re-adding the sources and Update index would rebuild.
  - **Safety**: Source files on disk untouched. Other collections unaffected. PDF structure cache stays (shared across collections).

---

## Cross-cutting principles

These apply to every confirm and disclosure screen:

1. **The "Cost" row is what the user pays.** If the action *gives*
   something (disk freed on uninstall, time saved on battery), use a
   different label (`Disk freed`, `Benefit`). Don't conflate.

2. **Always answer "what about X"** when X is something the user knows
   exists and might fear is affected. For pdf-structure that's the
   cache. For Delete collection that's source files. For Clear cache
   that's the search index.

3. **Completion copy matches the action's verb.** "Restart to use it"
   on Install; "Restart to apply" on Uninstall.

4. **Never leak implementation detail** (raw package versions, internal
   directory names, extractor signatures). User-facing language only.

5. **Map the after-state.** Tell the user what they need to do next, if
   anything. (Restart fnd; Run Update index; etc.)

## Implementation status

| Workflow | Confirm body | Completion copy | Wired through |
|---|---|---|---|
| 1. Install pdf-structure | ✓ matches audit | ✗ generic — needs "Update index next" hint | ✓ |
| 2. Uninstall pdf-structure | ✓ matches audit | ✓ "Restart to apply" | ✓ |
| 3. Update index (per-collection) | n/a (no confirm — safe action) | n/a | ✓ |
| 4. Update all collections | ✓ matches audit | n/a | ✗ stub — only first collection runs |
| 5. Update cache | ✓ matches audit | n/a | ✗ stub — Phase F |
| 6. Prune stale | ✓ matches audit | ✓ | ✓ |
| 7. Clear cache | ✓ matches audit | ✓ | ✓ |
| 8. Auto-resume toggle | n/a (toggle) | n/a | ✓ |
| 9. Cache-at-index-time toggle | n/a (toggle) | n/a | ✓ |
| 10. Delete collection | ✗ doesn't reassure about source files | ✓ | ✓ |

**Outstanding work after this audit:**
- Wire Update all collections sequential chain.
- Wire Update cache action (currently a stub).
- Improve Install completion copy with the "Update index next" hint.
- Improve Delete collection confirm with the source-files reassurance.
- Tighten Auto-resume and Cache-at-index-time toggle descriptions per
  this audit.
