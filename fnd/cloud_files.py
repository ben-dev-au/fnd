"""Cloud-backed placeholder files — is this file's data actually here?

Fourth platform seam, alongside :mod:`fnd.paths` (where files live),
:mod:`fnd.launcher` (how to open them) and :mod:`fnd.os_labels` (what the
platform calls things). This one answers "are the bytes local, or will
touching this file pull them down over the network?".

Every sync client (iCloud Drive, OneDrive, Dropbox, Google Drive) can leave
a *placeholder*: a normal-looking directory entry whose contents are not on
disk. Opening one blocks until the provider materialises it — seconds for a
note, minutes for a large PDF. The indexer needs to know that up front so it
can tell the user what it is waiting for instead of looking wedged.

Detection is a filesystem-attribute question, never a path question. On
macOS an evicted file carries ``SF_DATALESS`` wherever it lives — inside
``~/Library/Mobile Documents``, under "Desktop & Documents Folders" sync, or
in a third-party provider mounted at ``~/Library/CloudStorage``. Attribution
(*which* service) is the only path-dependent part, and it is cosmetic: it
names the provider in a progress line and degrades to a generic label.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_FETCH_TIMEOUT_S",
    "CloudFetchError",
    "FetchWait",
    "Materialisation",
    "current_wait",
    "fetch",
    "is_placeholder",
    "materialisation",
    "provider_label",
    "reset_wait",
]

# Per-file ceiling on a cloud fetch before the file is called blocked.
# Generous: a large PDF on a slow link legitimately takes a while, and the
# user is watching a progress line that says so. Overridden by
# ``defaults.cloud_fetch_timeout_s``.
DEFAULT_FETCH_TIMEOUT_S: Final = 60.0


class Materialisation(StrEnum):
    """Whether a file's bytes are on local disk."""

    LOCAL = "local"
    """Bytes are present; reading is a plain disk read."""

    PLACEHOLDER = "placeholder"
    """Cloud-backed stub; reading blocks on a download."""

    UNKNOWN = "unknown"
    """Platform exposes no placeholder marker. Treated as local."""


# macOS: st_flags bit set on an evicted (dataless) file.
_SF_DATALESS: Final = 0x40000000

# Windows: st_file_attributes bits a sync provider sets on a placeholder.
# OFFLINE is the classic HSM bit; the RECALL_* pair is what the modern
# Cloud Files API (OneDrive, Dropbox, Google Drive) uses for files that are
# online-only or dehydrated.
_FILE_ATTRIBUTE_OFFLINE: Final = 0x00001000
_FILE_ATTRIBUTE_RECALL_ON_OPEN: Final = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: Final = 0x00400000
_WINDOWS_PLACEHOLDER_BITS: Final = (
    _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

# Bound once so a test can swap the stat call without patching the shared
# ``os`` module, which would hand a fake back to every caller in the
# interpreter — pytest's own bookkeeping included.
_stat = os.stat

# Path separators, both spellings: a Windows path inspected on POSIX (or a
# fixture carrying one) must still split into components.
_SEP_RE: Final = re.compile(r"[/\\]")

_GENERIC_PROVIDER: Final = "cloud storage"

# Provider names, matched against the START of a path component — never as
# a bare substring, which made "box" claim ~/Documents/Inbox and Sandbox.
# A prefix still catches the real spellings, which append an account:
# "OneDrive-Personal", "OneDrive - Contoso", "GoogleDrive-user@host".
# macOS mounts third-party FileProvider domains under ~/Library/CloudStorage;
# Windows sync clients use a top-level folder named after the service.
_PROVIDER_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("mobile documents", "iCloud Drive"),
    ("com~apple~clouddocs", "iCloud Drive"),
    ("onedrive", "OneDrive"),
    ("dropbox", "Dropbox"),
    ("googledrive", "Google Drive"),
    ("google drive", "Google Drive"),
    ("box", "Box"),
)


def materialisation(path: Path) -> Materialisation:
    """Whether ``path``'s bytes are local, remote, or unknowable here.

    Never raises and never opens the file — a stat is enough, and opening
    is the very thing callers are trying to decide about.
    """
    try:
        st = _stat(path)
    except OSError:
        # Missing / unreadable: not our question to answer. Callers get the
        # real error when they try to read it.
        return Materialisation.UNKNOWN

    if sys.platform == "darwin":
        flags = getattr(st, "st_flags", 0)
        return Materialisation.PLACEHOLDER if flags & _SF_DATALESS else Materialisation.LOCAL
    if sys.platform == "win32":
        attrs = getattr(st, "st_file_attributes", 0)
        return (
            Materialisation.PLACEHOLDER
            if attrs & _WINDOWS_PLACEHOLDER_BITS
            else Materialisation.LOCAL
        )
    # Linux/BSD: sync clients here (rclone mounts, Insync, Nextcloud) expose
    # no common marker, so there is nothing honest to report.
    return Materialisation.UNKNOWN


def is_placeholder(path: Path) -> bool:
    """True only when the platform positively says the bytes are remote.

    ``UNKNOWN`` reads as "assume local" — the alternative would have Linux
    treat every file as cloud-backed.
    """
    return materialisation(path) is Materialisation.PLACEHOLDER


def provider_label(path: Path) -> str:
    """Human name of the sync service backing ``path``, for progress text.

    Best-effort: a path heuristic over the mount conventions each client
    uses. Anything unrecognised (notably macOS "Desktop & Documents
    Folders" sync, which leaves files at their ordinary paths) falls back
    to a generic label. Detection does not depend on this being right.
    """
    parts = _SEP_RE.split(str(path).casefold())
    for marker, label in _PROVIDER_MARKERS:
        if any(part.startswith(marker) for part in parts):
            return label
    # Desktop & Documents sync keeps the ordinary path, so on macOS an
    # otherwise-unattributed placeholder is iCloud far more often than not.
    if platform.system() == "Darwin":
        return "iCloud Drive"
    return _GENERIC_PROVIDER


# ── Waiting on a fetch ───────────────────────────────────────────────
#
# A fetch blocks the worker thread that asked for it, so no progress event
# can be emitted while it runs. The UI's periodic tick reads this snapshot
# instead, which is what turns "the app is frozen" into "waiting 9s for
# iCloud Drive to send Week 7 Notes.md".


class CloudFetchError(TimeoutError):
    """A cloud fetch exceeded its deadline; treat the file as blocked."""


@dataclass(frozen=True, slots=True)
class FetchWait:
    """The fetch currently blocking a worker, if any."""

    path: str
    provider: str
    started_monotonic: float

    def seconds_waiting(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)


_wait_lock = threading.Lock()
_wait: FetchWait | None = None


def current_wait() -> FetchWait | None:
    """The in-flight fetch, or None. Safe to call from any thread."""
    with _wait_lock:
        return _wait


def reset_wait() -> None:
    """Forget any in-flight fetch. Called between runs so a stale record
    can't leak into the next one's display."""
    global _wait
    with _wait_lock:
        _wait = None


def fetch[T](call: Callable[[], T], *, path: Path, timeout_s: float) -> T:
    """Run ``call`` — which will materialise ``path`` — under a deadline.

    Publishes a :class:`FetchWait` for the duration so the UI can name what
    it is waiting on, and raises :class:`CloudFetchError` if the provider has
    not delivered within ``timeout_s``.

    The worker is a throwaway daemon thread rather than a pooled one: a
    timed-out fetch is still blocked in the kernel and would otherwise hold
    a pool slot for the rest of the run. Abandoning it costs one idle thread
    that unwinds whenever the provider finally answers.
    """
    global _wait

    result: list[T] = []
    failure: list[BaseException] = []

    def _run() -> None:
        try:
            result.append(call())
        except BaseException as e:
            failure.append(e)

    mine = FetchWait(str(path), provider_label(path), time.monotonic())
    with _wait_lock:
        _wait = mine
    worker = threading.Thread(target=_run, daemon=True, name=f"fnd-fetch-{path.name}")
    worker.start()
    worker.join(timeout_s)
    try:
        if worker.is_alive():
            raise CloudFetchError(
                f"{provider_label(path)} did not deliver the file within {int(timeout_s)}s"
            )
        if failure:
            raise failure[0]
        return result[0]
    finally:
        # Only retract our own record. The runner serialises fetches today,
        # but if two ever overlap, clearing unconditionally would drop the
        # "waiting on…" line for a fetch that is still blocked.
        with _wait_lock:
            if _wait is mine:
                _wait = None
