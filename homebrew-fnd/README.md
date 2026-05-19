# homebrew-fnd

Starter template for the public Homebrew tap. **Move this folder out
to a separate `<owner>/homebrew-fnd` GitHub repo before the first
release** — Homebrew's `brew tap <owner>/<name>` expects a top-level
repo, not a subdirectory.

End-user install path once the tap repo is live:

```sh
brew tap <owner>/fnd
brew install fnd
```

## What the formula does

`Formula/fnd.rb` uses `Language::Python::Virtualenv` to install fnd
from the PyPI sdist into a Homebrew-managed venv. No Apple
codesigning is involved (and none is required — see SECURITY.md in
the main repo for the rationale).

The release workflow (`.github/workflows/release.yml` in the main
repo) opens a PR against this tap on every `v*` tag, bumping `url`
and `sha256` to the new PyPI sdist.

## One-time setup

1. Move this directory to `<owner>/homebrew-fnd` on GitHub
   (top-level repo, public).
2. Configure the main `fnd` repo's release workflow:
   - Repository variable `HOMEBREW_TAP_REPO` =
     `<owner>/homebrew-fnd`.
   - Repository secret `HOMEBREW_TAP_PAT` = a fine-scoped GitHub PAT
     with PR-write access to the tap repo only.
3. Run `brew update-python-resources fnd` locally once after the
   first PyPI publish to materialise `resource` blocks for fnd's
   runtime deps. (Subsequent dep-pin changes refresh the same way.)
4. `brew audit --strict --new fnd` before merging the first PR;
   `brew audit --strict fnd` on each subsequent bump.

## Verifying the install

`brew install fnd` runs unsigned. Users can independently verify the
sdist via:

```sh
brew fetch fnd --force
shasum -a 256 "$(brew --cache fnd)"
```

…and compare to the `sha256` line in `Formula/fnd.rb`. For SLSA
provenance verification:

```sh
gh attestation verify "$(brew --cache fnd)" --repo <owner>/fnd
```
