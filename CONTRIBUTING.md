# Contributing to fnd

fnd is a document-search CLI/TUI developed on macOS; the Linux and Windows
builds are early beta with almost no real-world use. Bug reports, app-catalogue
entries and focused PRs are welcome, especially from Linux and Windows, where
the coverage gap is.

## Development setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/ben-dev-au/fnd.git
cd fnd
make sync          # uv sync --all-extras --group dev
make install-hooks # pre-commit hooks (ruff + pyright)
```

Run fnd from the checkout with `uv run python -m fnd <command>`. Only CI
exercises Linux and Windows, so there you will likely be first to hit whatever
is broken.

## Before opening a PR

```sh
make test   # full pytest suite
make lint   # ruff + pyright (strict)
make fmt    # auto-format and apply safe fixes
```

CI runs the suite on `macos-14`, `ubuntu-latest` and `windows-latest`, plus
ruff-format and pyright (strict). A green matrix means the code runs on all
three, not that anyone has checked the behaviour outside macOS.

Keep PRs scoped to one change. Platform-specific behaviour belongs in one of
the four seams, not in `sys.platform` checks scattered through feature code:
`fnd/paths.py` (where files live), `fnd/launcher.py` (opening and revealing),
`fnd/os_labels.py` (what the OS calls things), and `fnd/cloud_files.py`
(cloud-backed placeholders).

## Conventions

**Spelling is Australian/British throughout**: identifiers, comments,
docstrings, documentation and commit messages (`sanitise`, `normalise`,
`colour`, `behaviour`, `serialise`, `centre`, `cancelled`). The one exception
is third-party API surface that dictates American spelling (Rich/Textual
`color`, CSS properties, JSON `serialize`); match the library there and keep
our own names British.

**Comments default to none, with a three-line budget.** A comment earns its
place only by stating what the code cannot: a constraint, an invariant, a
measured number, or the bug it guards against. Well-named identifiers do not
need narrating. Going longer needs a specific reason, such as a table of
measurements whose numbers are the content, or a module docstring carrying
architecture; comments inside a function body essentially never qualify.

**State the fact, not the story.** No history ("this used to be X"), no arguing
for the change, no recap of the symptom that prompted it; those belong in the
commit message. Architecture rationale lives in the module docstring once, and
functions point at it rather than restating it. Function docstrings and tests
get one line stating the contract.

## Writing tests against the TUI

Almost every flaky test here was one mistake: treating a number of event-loop
turns as a proxy for "the async work finished". `pilot.pause()` means one pump
cycle happened, not that a decode and a mount are done, and how many cycles
that takes is a property of the machine.

It is not a starvation problem. Across 185 CI pytest jobs, failing jobs ran
+0.3 / -1.0 / +0.1 minutes against the median passing job on macOS / Ubuntu /
Windows: failures do not cluster in slow jobs. Windows fails about four times
as often for its own reasons, and every timed-out wait that printed its poll
count showed the poller alive (1400-plus polls in 30s) with the product simply
unfinished. More budget would have helped none of them.

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
    message="the cursor move never previewed the file it moved to",
)
```

A generous budget costs nothing: `wait_until` returns the moment the predicate
holds, so only a real failure spends it. Adding pauses or raising a sleep moves
the odds without removing the guess. No product signal to gate on is a gap in
the product, not the test.

**A gate that cannot fail is a deleted pause.** `wait_until` evaluates its
predicate before the first pause, so one already true returns without yielding.
A helper asking whether *any* preview existed was permanently true, courtesy of
the app's own cursor-park load: 14 sites that had each replaced a real
`pilot.pause()` returned in 0.0ms. Name the specific thing, such as the file
you navigated to, the count you expect, or the row that must exist.

Find them by wrapping `tests._pilot_wait.wait_until` from a `-p` plugin,
recording per call site whether the predicate was already true on first
evaluation. Patch it in `pytest_configure`: test modules bind the name at
import, so an autouse fixture is too late and silently measures nothing. It
gives a shortlist, not a verdict; whether pre-trigger state already satisfied
the predicate is a per-site fact.

**Do not change a test that has not failed.** Every regression shipped here
while fixing flakes came from sweeping sites chosen by pattern-match rather
than by evidence: the 14 gates above landed in six files with zero CI failures
between them, and an earlier regex hit two `action_open_command_palette()`
calls where the action *closes* the menu. Get the frequency table first, fix
what it names, and prove it on the same instrument. One green run is not
evidence: a head that failed four of six attempts displays as green, because
re-running overwrites a run's conclusion.

That bans a *speculative* sweep, not a grep. Once a failure proves a shape,
grepping for it and fixing every instance is evidence-driven, and closed the
mount-gated family here in one pass instead of one flake at a time. Three of
those had never failed on their own and fixing them was still right. With
nothing failed you have a hunch, and a hunch is what the fourteen gates were.

Three more shapes worth recognising, all shipped here:

- **A precondition that depends on losing a race.** "The cache is still cold"
  was true locally and false on a differently-timed runner, because the
  cursor-park load starts a coverage sweep that decodes neighbours. Make the
  state and assert you made it.
- **A boolean "in progress" flag used as a completion signal.** "Not started"
  and "finished" read the same. Compare a monotonic counter against a value
  read before the trigger.
- **A derived cache with no invalidation event.** Assert the invariant (the
  cache agrees with a fresh reading), not the value; a stale cache and a wrong
  computation look identical.

### Reproducing a slow runner locally

A green local run says little; the runners are slower and Windows slowest. In
rough order of fidelity:

- `docker run --cpus=2` against `ubuntu-latest`, the only option here that is a
  genuinely constrained Linux rather than a simulation. Untested so far: it
  needs a container runtime started first.
- The suite under CPU contention (busy loops on most cores), which reproduced a
  live CI flake on unmodified `main`.
- A delay injected into the preview decode, modelling background work taking
  longer, which is the Windows shape.
- Textual's `_wait_for_screen` bound shortened to ~0, modelling a starved pump,
  which enumerates every test that needs a pause to do real work.

The last two have opposite blind spots: shortening the wait removes *waiting*
without adding *work*, so a test that needs real wall-clock passes there and
still fails on CI. Neither models Windows; use CI for that.

## Measuring a flake

One CI run is one sample and cannot tell "fixed" from "lucky". Re-running makes
it worse: `gh run rerun` reuses the run id and overwrites its conclusion, so a
run that failed four times and passed once displays as green. Read attempt
history with
`gh api repos/<owner>/<repo>/actions/runs/<id>/attempts/<n> --jq .conclusion`.

Run the suite many times instead:

```
git push origin HEAD:flake-hunt/<name>     # 15 suites, 5 per OS, ~60 min
```

The workflow ranks failures and separates what a single run conflates: red in
**every** run of an OS is a platform failure and belongs in the gate; red in
**some** is a flake, listed by frequency. It also names any suite that reported
nothing, since a job dying before upload leaves no artifact and would otherwise
shrink the sample silently. `.github/scripts/aggregate_junit.py` runs
standalone over a directory of downloaded artifacts too.

**Pick the sample size before you see the result.** Five clean runs bound the
failure rate under roughly 45%, not at zero, which will not clear a 1-in-15
flake. This suite has been measured at 60 runs: two rounds on the very same
commit came back 5/5 and then 4/5, and the round that would have been called
"fixed" was the one that happened to be clean.

**Prove a fix with something that makes it fail on demand.** Without that
control a fix is unverified however green it looks, and "it passes now" is
equally consistent with "the test never exercised the change".

## Adding an app to the "Open with…" catalogue

Third-party app integrations live in [`docs/apps.md`](docs/apps.md). Add your
`[apps.<id>]` config block to the catalogue; see that page for the schema and
safety rules.

## Security

Please **do not** open public issues for security-sensitive findings. See
[`SECURITY.md`](SECURITY.md) for private reporting and the threat model.
