# Pre-public-release security audit & remediation

Branch: `security/public-release-audit`. Started 2026-05-19.

This document tracks the audit findings and remediation work for taking
`fnd` from a personal tool to a publicly-distributed macOS app
(Homebrew/signed binary + PyPI). The full audit narrative — threat
model, rationale per finding, sequencing — lives in the discussion
below. The checklist at the top is the live progress tracker.

## Progress

### MUST-fix before public release

- [x] **M1** — Drop AppleScript opener to close filename-injection
      vector. (`fnd/opener.py`,
      `tests/test_opener_injection.py`)
- [x] **M2** — Tighten dependency version pins; document update cadence.
      (`pyproject.toml`, `SECURITY.md`)
- [x] **M3** — Add CI dependency / secret audit workflow.
      (`.github/workflows/security.yml` — pip-audit + gitleaks; ruff-S
      pulled until N5 is fully addressed.)
- [x] **M4** — Write `SECURITY.md` (disclosure policy, scope).
- [x] **M5** — Decompression-bomb guards on DOCX/PPTX.
      (`fnd/extract/_limits.py`, `fnd/extract/_ooxml.py` *new*,
      `fnd/extract/docx.py`, `fnd/extract/pptx.py`,
      `tests/test_extract_safety.py`)
- [x] **M6** — Wrap every extractor entry; surface errors via stderr.
      (`fnd/extract/base.py`, `fnd/extract/{pdf,docx,pptx,markdown}.py`,
      `fnd/index.py`, `tests/test_extract_safety.py`)
      *Followup:* dedicated `fnd status --extract-errors` subcommand
      (stderr is the v1 surface).
- [x] **M7** — 0o700/0o600 perms on every state dir and file.
      (`fnd/_perms.py` *new*, `fnd/config.py`, `fnd/state.py`,
      `tests/test_perms.py`)

### SHOULD-fix before public release

- [ ] **S1** — Publish a Homebrew tap (`homebrew-fnd`) whose formula
      installs the PyPI sdist into a venv. **No Apple Developer ID
      required:** the formula is pure-Python, Homebrew downloads via
      curl (no quarantine xattr → Gatekeeper does not fire), and the
      arm64 wheels we depend on (pymupdf, tantivy) already ship with
      the ad-hoc signing they need. The original "codesign + notarize"
      track only becomes necessary if we later ship a bundled Mach-O
      via PyOxidizer/Briefcase, and even then ad-hoc signing
      (`codesign --sign -`) suffices for Homebrew distribution.
      (`.github/workflows/release.yml`, separate `homebrew-fnd/` repo)
- [ ] **S2** — Emit SBOM (CycloneDX) and SLSA provenance with each
      release. (release workflow) Independent of S1's signing
      decision; still valuable so users can verify *what's in the
      tarball* and *that GitHub Actions built it, not a compromised
      laptop*.
- [x] **S3** — Collection-name regex validator, applied at write and at
      query-DSL expansion. (`fnd/config.py`, `fnd/cli.py`,
      `tests/test_collection_name_validation.py`)
- [x] **S4** — Query length / clause-count caps.
      (`fnd/extract/_limits.py`, `fnd/query.py`,
      `tests/test_query_limits.py`)
- [x] **S5** — Reject symlink roots unless explicit; explicit
      `recurse_symlinks=False`. (`fnd/walk.py`,
      `tests/test_walk_symlinks.py`)
- [x] **S6** — Reject encrypted/password-protected PDFs and DOCX/PPTX
      cleanly. (`fnd/extract/pdf.py`, `fnd/extract/docx.py`,
      `fnd/extract/pptx.py`, `tests/test_extract_safety.py`)
- [x] **S7** — Frontmatter size caps. (`fnd/frontmatter.py`,
      `tests/test_frontmatter_limits.py`)

### NICE-to-have (defense in depth)

- [ ] **N1** — `sandbox-exec` extractor subprocess wrapper.
- [ ] **N2** — `hypothesis` fuzz harness for extractors.
      (`tests/fuzz/test_extractor_fuzz.py`, `tests/fixtures/malformed/`)
- [ ] **N3** — Remove dead `loguru` dep (or start using it).
      (`pyproject.toml`)
- [ ] **N4** — Implement OCR or remove the dead `ocr: bool` config knob.
      (`fnd/config.py`, possibly `fnd/extract/pdf.py`)
- [ ] **N5** — Tighten ruff to `--select S,B,A`. 48 findings on `fnd/`
      today; partial credit for `usedforsecurity=False` on the
      content-addressing SHA-1 calls. Re-enable
      `.github/workflows/security.yml` ruff job when the remaining 42
      findings are addressed or explicitly `noqa`-tagged.
- [ ] **N6** — Reproducible-build documentation in `SECURITY.md`.

---

## Threat model

We defend against, in priority order:

1. **Malicious document indexed by the victim.** Realistic. An attacker
   emails a PDF, the victim drops it in their `~/Papers` collection,
   `fnd` reads it during the next index pass. Parser exploits
   (memory-safety bugs in `pymupdf` / `python-docx` / `python-pptx`),
   decompression bombs, XML entity expansion, AppleScript injection
   via crafted filenames.
2. **Supply-chain compromise.** Realistic. A typo-squatted PyPI
   package, a transitive dep takes a malicious version bump, a
   Homebrew tap commit gets poisoned, a GitHub release asset is
   swapped.
3. **Adversarial config / state files** sitting in `~/Library/
   Application Support/fnd`. Lower risk (requires local code-exec
   already to plant them) but cheap to harden.
4. **Adversarial query string.** Very low real-world risk on a local
   tool — the user types their own queries. Worth checking ReDoS /
   injection only as a code-hygiene exercise.

Out of scope: network attackers (no network code), other-user-on-same-
Mac attackers (macOS already isolates home dirs; `fnd` is not a
multi-tenant service), GPU/driver exploits, side-channel.

---

## Findings & remediation

### MUST-fix

**M1. AppleScript injection via crafted filename.** `fnd/opener.py:67-77`
escapes `\` and `"` in POSIX paths fed into `osascript`, but macOS
allows newlines, carriage returns, and other control chars in
filenames. A filename containing `"\ntell application "Terminal" to do
script "..."` — or any newline — breaks out of the AppleScript string
literal and executes arbitrary AppleScript when the user hits "open"
on the search result. Attacker plants such a file in an indexed
directory; `fnd` indexes it; user clicks; payload runs.
*Fix:* drop AppleScript entirely in favour of the URL form
(`open_pdf_via_url`, already implemented at `fnd/opener.py:80-84`),
which goes through `subprocess.run(["open", url], …)` after
`urllib.parse.quote` — no escaping required, no shell-out. Delete
`open_pdf_via_applescript` and the `applescript` strategy enum value.
Tests: `tests/test_opener_injection.py` with filenames containing
newline, tab, `"`, backslash, NUL — assert the URL is well-formed.

**M2. Pinned-range dependencies allow unbounded upgrades.**
`pyproject.toml` uses `>=` ranges for every runtime dep
(`pymupdf>=1.27`, `python-docx>=1.1`, etc.). `uv.lock` does pin
concrete versions, but a contributor running `uv sync` after
`uv lock --upgrade` silently pulls new majors. For a security-
sensitive parser like `pymupdf` (history of arbitrary-read and
use-after-free CVEs), we want explicit pins and a documented update
cadence.
*Fix:* tighten ranges to `~=` where safe and pin majors:
`pymupdf~=1.27`, `pymupdf4llm~=0.0.17`, `python-docx~=1.1`,
`python-pptx~=1.0`, `tantivy~=0.25`. Keep `uv.lock` as source of
truth; run `uv lock --upgrade-package <name>` quarterly (documented
in `SECURITY.md`). Add `scripts/audit-deps.sh` calling
`pip-audit -r <(uv export)` — wired into the weekly GitHub Action
in M3.

**M3. No CI dependency audit.** No `pip-audit` / `osv-scanner` /
Dependabot runs against `uv.lock`. Today we will find out about a
new CVE from Twitter, not from CI.
*Fix:* `.github/workflows/security.yml` on push + weekly cron:
- `pip-audit --strict` on exported requirements.
- `gitleaks detect` for committed secrets.
- `trufflehog filesystem .` as a second secret pass.
- `ruff check --select S` (bandit-equivalent ruleset).
Build fails on any HIGH/CRITICAL CVE in a direct dep.

**M4. No SECURITY.md / disclosure policy.** Required for any public
repo that ingests untrusted data. Without it, researchers either
disclose publicly or give up.
*Fix:* write `SECURITY.md` with: supported versions, how to report
(email; PGP key optional), 90-day disclosure window, scope
("we ingest untrusted documents; parser bugs in upstream libs are in
scope, exploits via crafted queries are out of scope as the local
user is the only query author"). Reference: GitHub's template. Link
from `README.md`.

**M5. Decompression-bomb protection on OOXML.** DOCX and PPTX are ZIP
archives. `python-docx` and `python-pptx` open them without validating
uncompressed size. A 1 MB DOCX can expand to 10 GB of XML and OOM the
indexer (and arguably the host). Same threat as the classic `42.zip`.
*Fix:* pre-check in `fnd/extract/docx.py:200` and
`fnd/extract/pptx.py:144` using `zipfile.ZipFile(path)` to inspect the
central directory before handing the path off:
```python
with zipfile.ZipFile(path) as zf:
    total_uncompressed = sum(zi.file_size for zi in zf.infolist())
    max_ratio = max((zi.file_size / max(zi.compress_size, 1)
                     for zi in zf.infolist()), default=1)
if total_uncompressed > LIMIT_OOXML_UNCOMPRESSED:
    raise ExtractRejected(path, "uncompressed > limit")
if max_ratio > LIMIT_OOXML_RATIO:
    raise ExtractRejected(path, "ratio > limit")
```
Constants in `fnd/extract/_limits.py` (new): 500 MB and 200× ratio
are conservative for legitimate office docs.

**M6. Extractor crash propagates and kills index build.** `pymupdf.open`
(`fnd/extract/pdf.py:229`), `Document()` (`docx.py:200`),
`Presentation()` (`pptx.py:144`), and `MarkdownIt.parse()`
(`markdown.py:106`) are unguarded. Plain text uses `errors="replace"`
(`plain.py:26`) — fine. Any parser exception aborts the whole
`build_index` run — a single malicious file denies indexing of an
entire collection. Worse, parser bugs that look like crashes are
sometimes memory-corruption bugs that *would* have been exploits if
the catch wasn't there.
*Fix:* wrap each extractor's outermost call in `try/except Exception`
(broad on purpose — parser libs raise everything from `RuntimeError`
to opaque C-extension errors). Convert to a new `ExtractError(path,
root_cause)`. Indexer (`fnd/index.py:140`) logs and continues.
`fnd status --extract-errors` prints rejected files.

**M7. Restrictive permissions on fnd's state directory.**
`fnd/config.py` creates `~/Library/Application Support/fnd` (and the
index dir, state dir, `state/scope.toml`) with the default umask —
typically 0o755 on macOS. The TOML config can contain absolute paths
to a user's private documents; on a shared Mac another local user
could read those paths. Cheap to fix.
*Fix:* after every `mkdir(parents=True, exist_ok=True)` in
`fnd/config.py:34, 199, 204, 308, 440` and `fnd/state.py:87`, call
`path.chmod(0o700)`. After every file write (`state.py:104`,
`config.py:329`, etc.) call `chmod(0o600)`. Centralise both in
`fnd/_perms.py:secure_mkdir` / `secure_write_text` so we don't drift.

### SHOULD-fix

**S1. Homebrew tap (no Apple Developer ID required).** The original
draft of this audit recommended codesigning + notarizing a bottle via
Apple's notary service. On further investigation — and confirmed
research from the project owner — that's not needed for `fnd`:

- Gatekeeper only enforces on Mach-O binaries that carry the
  `com.apple.quarantine` extended attribute. The xattr is set by
  user-space apps that participate in LaunchServices' "I came from the
  internet" hand-off (Safari, Mail, Messages, AirDrop). `curl`,
  `wget`, and `brew install` do *not* set it. So a Homebrew-installed
  artifact never trips Gatekeeper, regardless of whether it's signed.
- `fnd` is a pure-Python CLI. There is no Mach-O *of ours* to sign in
  the first place. The Python deps that contain native code
  (`pymupdf`, `tantivy`) ship as wheels from PyPI with the ad-hoc
  signing they need to run on Apple Silicon — those wheels' authors
  handled that, not us.
- The minimum bar Apple Silicon enforces is that any arm64 Mach-O
  carries *some* signature, even an ad-hoc one. Ad-hoc signing
  (`codesign --sign -`) is free and requires no Apple Developer
  Program enrolment. Homebrew applies it automatically when bottling.

*Recommendation: two channels, both free.*
1. **PyPI / `pipx install fnd`.** Pure-Python install path; works
   on macOS, Linux, anywhere with Python 3.13. Set up a PyPI Trusted
   Publisher via OIDC so the release workflow can publish without an
   API token.
2. **Homebrew tap `homebrew-fnd`.** A `Formula/fnd.rb` that uses
   `Language::Python::Virtualenv` to install from the PyPI sdist:
   ```ruby
   class Fnd < Formula
     include Language::Python::Virtualenv
     desc "Fast, free, keyboard-driven document search for macOS"
     homepage "https://github.com/<owner>/fnd"
     url "https://files.pythonhosted.org/.../fnd-X.Y.Z.tar.gz"
     sha256 "..."
     depends_on "python@3.13"
     def install
       virtualenv_install_with_resources
     end
   end
   ```
   No signing step. `.github/workflows/release.yml` on a `v*` tag:
   (a) builds + uploads the sdist to PyPI, (b) downloads it back,
   computes SHA-256, (c) opens a PR against `homebrew-fnd` bumping
   `url` + `sha256`. The tap repo is just a GitHub repo —
   `brew tap <owner>/fnd && brew install fnd` is the end-user path.

The "PyOxidizer + codesign + notarize" track stays on the shelf and
only gets unboxed if we ever decide to ship a standalone Mach-O for
non-Homebrew users.

**S2. SBOM + release-artifact provenance.** At release time emit (a)
a CycloneDX SBOM via `cyclonedx-py` and (b) SLSA-style provenance
via GitHub's `actions/attest-build-provenance`. Lets users verify
exactly what's in the binary and that GitHub Actions (not an
attacker's laptop) built it.

**S3. TOML key injection on collection name.** `fnd/config.py:329`
inserts `collection_name` directly as a `tomlkit` table key. A name
containing `]`, `=`, or a newline produces a malformed config (foot-
gun) or — worst case — coaxes an extra top-level table.
*Fix:* validate in `fnd/cli.py collection add` and in
`CollectionConfig.__init__`: regex `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$`,
reject otherwise with a clear error. Same regex in
`fnd/query_dsl.py:_expand_collection_shorthand` to keep the two in
lock-step. Property-based test via `hypothesis` (already a dev-dep).

**S4. Bound query complexity.** `fnd/query_dsl.py` and Tantivy's
parser will compile a 100 KB query with deeply nested boolean groups.
Today the only consumer is the user themselves, but once we add
`--query-from-file` or accept queries from a URL handler / Spotlight
integration this becomes a DoS knob.
*Fix:* in `Searcher.search` (`fnd/query.py:256`), reject queries over
8 KB and over 64 boolean clauses with a clear error. Limits in
`fnd/_limits.py`. Hypothesis test asserting pathological queries
reject before hitting Tantivy.

**S5. Symlink handling clarity.** `fnd/walk.py:76` resolves the
collection root through any symlink (so a root that *is* a symlink to
`/etc` gets followed); inside the root, `p.is_symlink()` short-circuits
file-level symlinks when `follow_symlinks=False` (default — good).
Python 3.13 `Path.rglob` does not follow directory symlinks by
default — but that's an implicit guarantee we shouldn't rely on.
*Fix:* (a) at root-resolution in `walk()`, if the *original* `root`
is a symlink, refuse unless `follow_symlinks=True`. (b) Pass
`recurse_symlinks=False` to `Path.rglob` explicitly. (c) Document the
semantics in `walk.py`'s module docstring.

**S6. Encrypted-PDF / password-protected DOCX rejection.** `pymupdf`
silently returns zero text for an encrypted PDF (data loss) or, for
some files, raises an opaque error mid-stream.
*Fix:* check `doc.is_encrypted` / `doc.needs_pass` at
`fnd/extract/pdf.py:229` and reject with `ExtractError("encrypted")`.
For DOCX/PPTX, `python-docx`/`python-pptx` raise `PackageNotFoundError`
on password-protected packages; convert to the same.

**S7. Catch & surface frontmatter bombs.** `fnd/frontmatter.py` is
hand-rolled, no YAML lib, so no `!!python/object` deserialization
risk — good. But the parser has no length-of-frontmatter cap; a
100 MB frontmatter block will OOM the extractor before Tantivy ever
sees it.
*Fix:* in `read_frontmatter_from_file`, refuse files where the
closing `---` fence is more than 64 KB from the start, or where any
single line exceeds 4 KB.

### NICE-to-have

**N1.** `sandbox-exec` extractor subprocess wrapper — denies network,
limits writes to a temp dir, caps address space; contains a parser
exploit to "crashed extractor, indexer continues." Phase-2 hardening.

**N2.** `hypothesis` fuzz harness for extractors. Random byte blobs
plus a corpus of malformed-but-parseable docs under
`tests/fixtures/malformed/` (seeded from oss-fuzz pymupdf corpora,
public domain). Nightly via GitHub Actions, capture crashing inputs.

**N3.** `loguru` is pinned in `pyproject.toml` but never imported. Dead
deps are attack surface for no benefit. Either start using it or
remove it.

**N4.** `fnd/config.py:152` accepts `ocr = true` in TOML; field is
silently ignored at extract time. Either implement OCR (argv-list
subprocess call to `ocrmypdf`) or remove the field and reject the key.

**N5.** Enable ruff `--select S,B,A` (bandit, bugbear, builtin-
shadowing).

**N6.** Reproducible builds. `uv sync --frozen` in CI; document the
exact toolchain in `SECURITY.md` so two people building from the
same tag get identical wheels. C-extension builds (`pymupdf`,
`tantivy`) need `SOURCE_DATE_EPOCH` and a documented compiler version.

---

## Distribution-hardening track (Homebrew tap + PyPI)

Separate workstream from the source-code fixes. The original draft
assumed an Apple Developer ID would be needed; on further
investigation, it isn't — both channels below cost nothing.

1. **PyPI Trusted Publisher.** Configure
   <https://pypi.org/manage/account/publishing/> with the GitHub repo
   and the release workflow's job name. The workflow then mints a
   short-lived OIDC token at release time; no API token sits in repo
   secrets to be stolen.
2. **Release workflow.** `.github/workflows/release.yml` on a `v*`
   tag:
   - `uv build` → sdist + wheel.
   - `cyclonedx-py environment` → CycloneDX SBOM (S2).
   - `actions/attest-build-provenance` → SLSA-style attestation
     (S2). Proves the artifact came from this commit on this
     workflow, not from a developer's laptop.
   - `pypa/gh-action-pypi-publish` (Trusted Publisher, no token)
     uploads the sdist + wheel to PyPI.
   - Compute SHA-256 of the sdist, open a PR against
     `homebrew-fnd/Formula/fnd.rb` bumping `url` and `sha256`.
   - Attach sdist + wheel + SBOM + provenance to the GitHub Release.
3. **Homebrew tap repo.** Create `<owner>/homebrew-fnd`. Its sole
   contents are `Formula/fnd.rb` plus a README. `brew audit --strict
   --new fnd` before opening the PR, `brew install
   --build-from-source ./Formula/fnd.rb` to dry-run.
4. **End-user install paths in README.md:**
   - `brew tap <owner>/fnd && brew install fnd`
   - `pipx install fnd`
   - For the security-conscious: `gh attestation verify
     <downloaded-tarball> --repo <owner>/fnd` confirms GitHub Actions
     produced it on the expected workflow.

Codesign + notarize via Apple Developer ID is *not* on this path.
It only matters if we later bundle a standalone Mach-O binary (e.g.
PyOxidizer) and ship it through a channel that quarantines downloads
(Safari, S3 + browser link). Pure-Python `pipx` and Homebrew installs
bypass quarantine entirely because the download tools (`pip`, `brew`)
do not set the `com.apple.quarantine` xattr.

---

## Verification

After implementation, the audit holds if:

1. **Static analysis clean.** `ruff check --select S` exits 0;
   `pip-audit --strict` exits 0; `gitleaks detect` exits 0.
2. **Unit tests pass.** `uv run pytest` green, including every new
   test file referenced above. Each MUST/SHOULD finding has at least
   one test that fails on `main` and passes on the fix.
3. **Adversarial corpus pass.** Build the corpus from public sources
   (fixed-upstream oss-fuzz pymupdf crashes, `42.zip`, a DOCX with
   10 MB of `<w:t>` repetition) under `tests/fixtures/malformed/`.
   `fnd index` against the dir completes without crashing, surfaces
   every rejection via `fnd status --extract-errors`, and the index
   contains zero of the malformed files. Capture timings — total
   time should not exceed 2× the same dir's clean-doc baseline.
4. **AppleScript-injection probe.** Manually create a file named
   `pwn"\necho HACKED > /tmp/fnd-pwn\n.pdf` in a test collection.
   Index, search, open. Confirm `/tmp/fnd-pwn` does not exist after.
5. **Permission audit.**
   `find ~/Library/Application\ Support/fnd -type d -perm -o+r` returns
   empty (no world-readable dirs); same with `-type f -perm -o+r`.
6. **Release-pipeline dry-run.** Push `v0.0.1-rc1`, watch
   `.github/workflows/release.yml` build sdist + wheel, attach SBOM
   and provenance, publish to PyPI via OIDC, and open the
   bump-PR against `homebrew-fnd`. Verify
   `gh attestation verify <downloaded-tarball> --repo <owner>/fnd`
   accepts the attestation. Verify the formula installs cleanly:
   `brew install --build-from-source ./Formula/fnd.rb`.
7. **SBOM sanity.** `cyclonedx-py` output lists every dep in
   `uv.lock`. `pip-audit` consumes the SBOM and finds zero open
   CVEs at release time.

---

## Sequencing

1. Source-code MUSTs first (M1, M5, M6, M7, S3, S7) — pure code
   changes, each lands as its own commit with its own test.
2. Dependency hygiene (M2, M3) — tightens what ships, no behaviour
   change.
3. Docs + disclosure (M4) — unblocks researchers reaching out.
4. Distribution pipeline (S1, S2, S5, S6, N6) — biggest single chunk;
   dry-run with a `v0.0.1-rc` tag, iterate on the workflow.
5. Polish (N1–N5) — schedule for v0.1 → v0.2.

Stop-the-press conditions for the public release: any unfixed MUST.
Everything else can ship "known and tracked."
