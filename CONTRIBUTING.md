# Contributing to fnd

Thanks for your interest. fnd is a macOS-only document-search CLI/TUI; it's
early but actively developed. Bug reports, app-catalogue entries, and focused
PRs are all welcome.

## Development setup

Requires macOS, Python 3.13, and [uv](https://docs.astral.sh/uv/).

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

CI runs the same checks on macOS. Keep PRs scoped to one change; match the
surrounding code's style and comment density.

## Adding an app to the "Open with…" catalogue

Third-party app integrations live in `docs/apps/`. To contribute one, add a
single file describing the `[apps.<id>]` config block — see
[`docs/apps/README.md`](docs/apps/README.md) for the schema and safety rules.

## Security

Please **do not** open public issues for security-sensitive findings. See
[`SECURITY.md`](SECURITY.md) for private reporting and the threat model.
