"""OS display vocabulary — what the running platform *calls* things.

Third seam alongside :mod:`fnd.paths` (where files live) and
:mod:`fnd.launcher` (how to open/reveal them). Those two answer "where" and
"how"; this one answers "what does the user call it", so help text, keybinding
rows, and app labels read natively instead of hardcoding ``Finder`` / ``Alt``
on every OS.

Deliberately uncached pure functions of :func:`platform.system` — the value
never changes within a process, and staying uncached means a test can
monkeypatch the platform without the ``cache_clear()`` bookkeeping
``launcher.get_launcher`` needs.
"""

from __future__ import annotations

import platform
from typing import Final

_DARWIN: Final = "Darwin"
_WINDOWS: Final = "Windows"

# Modifier spelling. Ctrl/Shift are the same everywhere; only Option and
# Command are glyphs on macOS, matching how Apple labels the physical keys.
_MODIFIERS_MAC: Final[dict[str, str]] = {
    "ctrl": "Ctrl",
    "shift": "Shift",
    "alt": "⌥",
    "cmd": "⌘",
}
_MODIFIERS_OTHER: Final[dict[str, str]] = {
    "ctrl": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "cmd": "Cmd",
}

_FILE_MANAGERS: Final[dict[str, str]] = {
    _DARWIN: "Finder",
    _WINDOWS: "File Explorer",
}
# Linux/BSD desktops each ship their own (Files, Dolphin, Thunar…), and
# `LinuxLauncher.reveal` picks whichever is installed at call time. Naming a
# specific one in help text would be wrong on most machines, so stay generic.
_FILE_MANAGER_FALLBACK: Final = "file manager"


def is_macos() -> bool:
    return platform.system() == _DARWIN


def is_windows() -> bool:
    return platform.system() == _WINDOWS


def file_manager_name() -> str:
    """Bare name of the platform file manager, for labels and titles."""
    return _FILE_MANAGERS.get(platform.system(), _FILE_MANAGER_FALLBACK)


def file_manager_phrase() -> str:
    """The file manager as it reads mid-sentence. "Finder" and "File Explorer"
    are proper nouns and take no article; the generic Linux fallback is a common
    noun, so it needs one — "reveals in file manager" is not English."""
    name = file_manager_name()
    return name if name != _FILE_MANAGER_FALLBACK else f"your {name}"


def reveal_label() -> str:
    """Title for the reveal action — "Reveal in Finder" and friends."""
    return f"Reveal in {file_manager_name()}"


def modifier_label(mod: str) -> str | None:
    """Display spelling of a Textual modifier token, or ``None`` when ``mod``
    isn't a modifier at all. Callers rely on the ``None`` to tell
    ``ctrl``-the-modifier from a plain key name."""
    table = _MODIFIERS_MAC if is_macos() else _MODIFIERS_OTHER
    return table.get(mod)


def alt_word() -> str:
    """The Option/Alt key spelled out, for prose ("hold Option and arrow").
    :func:`modifier_label` gives the glyph used in key columns."""
    return "Option" if is_macos() else "Alt"


# ── Help-text placeholders ───────────────────────────────────────────────
#
# Static help tables (the action registry, the keybinding cheat-sheet tables,
# the footer hint clusters) are module constants built at import time, so they
# can't call the functions above and stay per-OS. They embed these tokens
# instead and the render path calls :func:`localise`, which keeps one
# vocabulary across every surface and one place to test it.

FILE_MANAGER: Final = "{file_manager}"  # → Finder / File Explorer / your file manager
REVEAL_LABEL: Final = "{reveal_label}"  # → Reveal in Finder / …   (row titles)
ALT_KEY: Final = "{alt_key}"  # → ⌥ / Alt              (key columns)
ALT_WORD: Final = "{alt_word}"  # → Option / Alt         (prose)


def localise(text: str) -> str:
    """Resolve the OS-vocabulary placeholders in ``text``.

    Literal token replacement, not :meth:`str.format` — help text is full of
    unrelated braces (``{60}`` proximity syntax, ``{path}`` app-argv
    templates) that formatting would mangle or raise on.
    """
    return (
        text.replace(REVEAL_LABEL, reveal_label())
        .replace(FILE_MANAGER, file_manager_phrase())
        .replace(ALT_KEY, modifier_label("alt") or "Alt")
        .replace(ALT_WORD, alt_word())
    )
