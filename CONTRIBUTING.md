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

## Adding an app to the "Open with…" catalogue

Third-party app integrations live in [`docs/apps.md`](docs/apps.md). To
contribute one, add your `[apps.<id>]` config block to the catalogue; see that
page for the schema and safety rules.

## Security

Please **do not** open public issues for security-sensitive findings. See
[`SECURITY.md`](SECURITY.md) for private reporting and the threat model.
