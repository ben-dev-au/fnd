# Security policy

## Reporting a vulnerability

Please report security issues privately. Email
**methyl-infills3l@icloud.com** with subject `fnd: <short description>`.
Expect an initial reply within 5 business days. If you'd like to encrypt
the report, attach a request for a PGP key and one will be returned.

Please do **not** open a public GitHub issue, draft pull request, or
discussion-thread comment for security-sensitive findings, even when
the underlying behaviour seems minor. Public disclosure is coordinated
after a fix ships.

## Disclosure window

We aim to acknowledge the issue, agree on a CVSS-ish severity, and
publish a fix within **90 days** of the initial report. If the report
involves a third-party dependency (pymupdf, python-docx, python-pptx,
tantivy, ...) and that upstream chooses a longer window, ours extends to
match, and we will not publish details of an unpatched upstream issue.

If you intend to publish independently regardless, please tell us when
so the public advisory and the patch can land together.

## Scope

In scope:

- Anything that lets an attacker move from "user indexes my document"
  to "code runs on the user's machine" or "data leaks off the user's
  machine." Parser exploits in our indexed file types (PDF, DOCX,
  PPTX, MD, TXT) sit at the top of the list, even when the root cause
  is in an upstream library we depend on.
- Anything that lets a non-owner local user read fnd's on-disk state.
  The state can disclose the paths (and partial content via the
  index) of the owner's documents.
- Filename / shell-argument injection through the "open in app"
  flow.
- Adversarial config files dropped in
  `~/Library/Application Support/fnd/` that escalate beyond the
  permissions of the current user.
- Supply-chain integrity of the published Homebrew bottle and PyPI
  wheel (codesigning bypass, mismatched SHA-256 in the formula,
  poisoned upstream release).

Out of scope (today):

- Network-layer attacks. fnd opens no sockets, listens on no ports,
  speaks no HTTP. If you find network activity, that is in scope;
  please report it.
- DoS via genuinely huge documents that exceed the configured size
  caps (`fnd/extract/_limits.py`). The cap *is* the defence; raise
  an issue if a real-world legitimate doc trips it.
- Theoretical query-DSL malice. fnd is a local tool whose queries
  are typed by the same user who owns the data. We do still bound
  query complexity as a hygiene exercise.
- Other-user-on-same-Mac escalation that depends on a setuid bug or
  a macOS isolation hole; those are macOS issues. We do harden the
  state directory to 0o700 so a different mechanism would be
  needed to reach our files in the first place.
- Side-channel attacks against indexed text.

## Supported versions

- Latest released version on the `main` branch: supported.
- Pre-release / `-rc` tags: supported until the corresponding `v*`
  ships, then drop.
- Older minor versions: best effort; we will fix where the diff is
  small but expect users to upgrade for everything else.

## Dependency hygiene

- Runtime deps use the three-component compatible-release operator
  (`~=X.Y.Z`) in `pyproject.toml`, so only patch releases float. These
  ranges — not `uv.lock` — are what an installer resolves against, and
  so they are what every user actually gets.
- `uv.lock` governs the development environment and the `uv sync
  --frozen` CI jobs. It is not shipped to installers: `pip`, `pipx`, `uv
  tool install` and the Homebrew formula all resolve the ranges above.
  `deps-latest.yml` is the deliberate exception — it runs `uv lock
  --upgrade` first, precisely to test outside the lock.
- CI runs `pip-audit --strict` against the exported lockfile on every
  push and weekly on a cron (`.github/workflows/security.yml`). The
  build fails on any HIGH/CRITICAL CVE in a direct dep.
- `.github/workflows/deps-latest.yml` runs weekly, resolves every
  dependency at its newest allowed version ignoring `uv.lock`, and runs
  the suite against it. With `~=X.Y.Z` pins that is the patch float —
  the versions reaching users with no PR to review, and this is their
  only gate. A new minor is gated instead by the Dependabot PR that
  raises the pin, which `ci.yml` runs the full suite against.
- Quarterly we run `uv lock --upgrade-package <name>` on each
  security-critical parser (pymupdf, python-docx, python-pptx,
  tantivy, markdown-it-py) and ship the bump as a patch release if
  the changelog discloses anything new.

## Reproducible installs

- `uv sync --frozen` reproduces the lockfile's resolution for a given
  Python version and platform. It reads `uv.lock` and refuses to resolve
  new versions; the wheels it selects still vary by platform.
- End-user installs (`pipx`/`uv tool install fndr`, Homebrew) resolve
  the `pyproject.toml` ranges instead. Those hold the major and minor
  series, but the patch release selected may change over time. Pass a
  constraints file if you need an exact set.
- Pinned toolchain: Python 3.13 (the one `uv python install 3.13`
  resolves), `uv >= 0.4`. CI uses the same.
- Pure-Python wheels build deterministically from the same sdist on
  the same Python minor. Native-extension deps (`pymupdf`, `tantivy`)
  ship as prebuilt wheels from PyPI; those are reproducible at the
  byte level for users on macOS arm64 / x86_64, identical to what CI
  uses.
- For a release built locally instead of via CI, set
  `SOURCE_DATE_EPOCH` to the commit timestamp before `uv build` so
  the sdist metadata matches what CI would produce.
- Tarball integrity: every GitHub Release ships a SHA-256 of the
  sdist and a SLSA build-provenance attestation. Verify with
  `shasum -a 256 fnd-<ver>.tar.gz` and
  `gh attestation verify fnd-<ver>.tar.gz --repo ben-dev-au/fnd`.
- For verifying a Homebrew tap install, the formula pins both `url`
  and `sha256`; `brew audit --strict` validates the pin matches the
  downloaded tarball.

## What we test

- Adversarial-document tests in `tests/test_extract_safety.py` cover
  decompression-bomb shape, parser-crash conversion, encrypted-PDF
  refusal, and "one bad file doesn't kill the index build."
- Filename-injection tests in `tests/test_opener_injection.py` cover
  the macOS-specific osascript / argv escape concerns.
- Symlink containment in `tests/test_walk_symlinks.py`.
- Collection-name regex in `tests/test_collection_name_validation.py`.
- Permission posture on the state dir in `tests/test_perms.py`.

If a CVE or behaviour you encounter isn't covered by one of those, a
test failing on `main` and passing on the fix is the right shape for a
report.
