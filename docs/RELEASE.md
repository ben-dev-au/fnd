# Release process

End-to-end checklist for cutting an `fnd` release across PyPI and the
Homebrew tap. The release workflow
(`.github/workflows/release.yml`) does the heavy lifting on every
`v*` tag; this doc covers the one-time setup and the manual gates.

## One-time setup

### 1. PyPI Trusted Publisher

PyPI Trusted Publishers use OIDC instead of API tokens — there's no
long-lived secret in the repo to leak.

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Add a **Pending Publisher** with:
   - PyPI project name: `fnd`
   - Owner: `<your-github-user>`
   - Repository name: `fnd`
   - Workflow filename: `release.yml`
   - Environment name: `pypi` (matches the `environment:` field in
     the `publish-pypi` job)
3. On the first successful `release.yml` run, PyPI converts it from
   "pending" to an active publisher.

In the GitHub repo: create a `pypi` Environment under Settings →
Environments. Set "Required reviewers" to yourself if you want a
manual gate before each publish.

### 2. Homebrew tap repo

1. Move the seeded `homebrew-fnd/` directory in this repo to a new
   top-level GitHub repo `<owner>/homebrew-fnd` (public). The tap is
   just a regular repo; Homebrew finds formulae under
   `Formula/*.rb`.
2. In the main `fnd` repo, under Settings → Secrets and variables →
   Actions:
   - Add a **Variable** `HOMEBREW_TAP_REPO = <owner>/homebrew-fnd`.
   - Add a **Secret** `HOMEBREW_TAP_PAT` — a fine-scoped Personal
     Access Token with `Contents: Read & Write` and `Pull requests:
     Read & Write` on `<owner>/homebrew-fnd` only.
3. End-users install via:
   ```sh
   brew tap <owner>/fnd && brew install fnd
   ```

### 3. (Optional) Apple Developer ID

**Not needed.** Homebrew downloads via curl, which does not set the
`com.apple.quarantine` xattr, so Gatekeeper never fires. fnd ships
no Mach-O binary of its own (it's pure Python wrapping native PyPI
wheels whose maintainers handle their own signing). The Apple
Developer Program ($99/yr) is required only if we later distribute
a bundled binary via a quarantining channel (e.g. browser
download). See `SECURITY.md` for the full rationale.

## Each release

1. **Bump the version** in `pyproject.toml`. Commit:
   ```sh
   git commit -m "chore(release): v0.1.0"
   ```
2. **Tag and push**:
   ```sh
   git tag v0.1.0
   git push origin main v0.1.0
   ```
3. **Watch `release.yml`** complete in Actions:
   - `build` builds sdist + wheel, emits a CycloneDX SBOM
     (`dist/sbom.cdx.json`), and records a SLSA build-provenance
     attestation against the artifacts (`actions/attest-build-
     provenance`).
   - `publish-pypi` uploads to PyPI via OIDC (no token).
   - `release-notes` opens the GitHub Release with sdist, wheel,
     and SBOM attached.
   - `bump-homebrew-tap` opens a PR against `<owner>/homebrew-fnd`
     with the new `url` and `sha256`.
4. **Merge the tap PR.** First time only:
   `brew update-python-resources fnd` locally and commit the
   resource blocks before merging — Homebrew needs them so
   `brew install fnd` can pin every transitive dep.
5. **Verify end-to-end:**
   ```sh
   brew untap <owner>/fnd 2>/dev/null || true
   brew tap <owner>/fnd
   brew install fnd
   fnd version
   gh attestation verify "$(brew --cache fnd)" --repo <owner>/fnd
   ```

## When something goes wrong

- **`pip-audit` fails on a transitive CVE**: bump the pinned dep
  via `uv lock --upgrade-package <name>`, regenerate, commit, and
  cut a patch release. The security workflow's `pip-audit` job runs
  on every push so this is the standard discovery surface.
- **PyPI Trusted Publisher rejects the upload**: confirm the
  Environment is exactly `pypi`, the workflow filename is exactly
  `release.yml`, and the repo + owner names match. Trusted
  Publishers do not match wildcards.
- **Homebrew tap PR can't be created**: usually the
  `HOMEBREW_TAP_PAT` has expired or is missing `Pull requests:
  Write` on the tap repo. Rotate, re-add as a secret, re-run the
  job from the Actions UI.
- **A user reports "Gatekeeper blocked fnd"**: this should not
  happen via `brew install`. Confirm they installed via brew and
  not by downloading the GitHub Release tarball with Safari (which
  *does* set the quarantine xattr). If they did, the fix is
  `xattr -dr com.apple.quarantine <path>` or installing via brew /
  pipx.
