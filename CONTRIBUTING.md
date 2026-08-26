# Contributing to fnd

Thanks for your interest. fnd is a document-search CLI/TUI developed on macOS,
with early-beta Linux and Windows builds that have had almost no real-world use.
It's early but actively developed. Bug reports, app-catalogue entries, and
focused PRs are all welcome — reports from Linux and Windows especially, since
that is where the coverage gap is.

## Development setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/). Development happens on
macOS; the other two OSes are only exercised by CI, so if you're set up to
develop on Linux or Windows you'll likely be the first to hit whatever is broken
there.

```sh
git clone https://github.com/ben-dev-au/fnd.git
cd fnd
make sync          # uv sync --all-extras --group dev
make install-hooks # pre-commit hooks (ruff + pyright)
```

Run fnd from the checkout with `uv run python -m fnd <command>`.

## Before opening a PR

```sh
make test   # full pytest suite
make lint   # ruff + pyright (strict)
make fmt    # auto-format and apply safe fixes
```

CI runs the suite on `macos-14`, `ubuntu-latest` and `windows-latest`, plus
ruff-format and pyright (strict). A green matrix means the code runs on all
three, not that the behaviour has been checked by a human anywhere but macOS.

Platform-specific behaviour belongs in one of the four seams rather than in
`sys.platform` checks scattered through feature code: `fnd/paths.py` (where
files live), `fnd/launcher.py` (opening and revealing), `fnd/os_labels.py` (what
the OS calls things), and `fnd/cloud_files.py` (cloud-backed placeholder files).

Keep PRs scoped to one change; match the surrounding code's style and comment
density.

## Writing tests against the TUI

Almost every flaky test this project has had was the same mistake: using a
number of event-loop turns as a proxy for "the async work finished".
`pilot.pause()` means one pump cycle happened, not that a decode and a mount are
done — and how many cycles that takes is a property of the machine. So
`await pilot.pause()` followed by an assertion on state that lands
asynchronously is a race whose odds are set by the runner. Windows loses it
most often because it is the slowest, not because it is Windows.

**Gate on the outcome you are about to assert.**

```python
# No: a guess about how fast this machine is.
rtree.move_cursor(node)
await pilot.pause()
assert app._preview.parent_id == want

# Yes: the product signal, with the budget as a safety net.
rtree.move_cursor(node)
await wait_until(
    pilot,
    lambda: app._preview.parent_id == want,
    timeout=30.0,
    message="the cursor move never produced a preview",
)
```

A generous budget costs nothing when the test passes — `wait_until` returns the
moment the predicate holds — and only spends time on a genuine failure. Adding
pauses or raising a sleep is never the fix; it moves the odds without removing
the guess. If there is no product signal to gate on, that is a gap in the
product, not in the test.

Three more shapes worth recognising, all of which have shipped here:

- **A precondition that depends on LOSING a race.** "The cache is still cold"
  was true locally and false on a differently-timed runner, because the
  cursor-park load starts a coverage sweep that decodes neighbours. Make the
  state and assert you made it, rather than hoping for it.
- **A boolean "in progress" flag used as a completion signal.** "Not started"
  and "finished" read the same. Compare a monotonic counter against a value read
  before the trigger.
- **A derived cache with no invalidation event.** Assert the invariant (the
  cache agrees with a fresh reading), not just the value — a stale cache and a
  wrong computation produce identical symptoms.

### Reproducing a slow runner locally

A green local run says little; the runners are slower and Windows is slowest.
In rough order of fidelity:

- `docker run --cpus=2` against `ubuntu-latest` — closest to the real thing,
  and the only option here that is a real constrained Linux rather than a
  simulation. Untested so far: it needs a container runtime started first.
- Run the suite under CPU contention (busy loops on most cores). This is what
  reproduced a live CI flake on unmodified `main`.
- Inject a delay into the preview decode — models background work taking
  longer, which is the Windows shape.
- Shorten Textual's internal `_wait_for_screen` bound to ~0 — models a starved
  pump, and enumerates every test that needs a pause to do real work.

The last two have opposite blind spots: shortening the wait removes *waiting*
without adding *work*, so a test that needs real wall-clock passes there and
still fails on CI. Neither models Windows; for that, see `dev/docs/DEV_VMS.md`
or CI itself.

## Adding an app to the "Open with…" catalogue

Third-party app integrations live in [`docs/apps.md`](docs/apps.md). To
contribute one, add your `[apps.<id>]` config block to the catalogue; see that
page for the schema and safety rules.

## Security

Please **do not** open public issues for security-sensitive findings. See
[`SECURITY.md`](SECURITY.md) for private reporting and the threat model.
