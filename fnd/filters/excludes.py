"""Exclude globs as the Index filters screen shows them: presets to tick, globs typed by hand.

A preset counts as on when every glob it ships is present; whatever is left over
was typed. Edits keep the list in the order written: an inherited list rewritten
in any other order would save as the source's own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def split_presets(globs: Iterable[str]) -> tuple[list[str], list[str]]:
    """``(preset keys on, globs typed by hand)`` for an excludes list."""
    from fnd.config import EXCLUDES_PRESETS

    remaining = list(dict.fromkeys(globs))
    on: list[str] = []
    for key, preset in EXCLUDES_PRESETS.items():
        shipped = preset["globs"]
        if all(g in remaining for g in shipped):
            on.append(key)
            remaining = [g for g in remaining if g not in shipped]
    return on, remaining


def _globs_of(keys: Iterable[str]) -> list[str]:
    from fnd.config import EXCLUDES_PRESETS

    picked = set(keys)
    return [g for k, p in EXCLUDES_PRESETS.items() if k in picked for g in p["globs"]]


def retick(excludes: Sequence[str], keys: Iterable[str]) -> tuple[str, ...]:
    """``excludes`` with exactly the presets ``keys`` on: unticked globs leave, new ones follow."""
    on, _typed = split_presets(excludes)
    wanted = set(keys)
    if set(on) == wanted:
        return tuple(excludes)
    dropped = set(_globs_of(set(on) - wanted)) - set(_globs_of(wanted))
    kept = [g for g in excludes if g not in dropped]
    return tuple(dict.fromkeys([*kept, *_globs_of(wanted - set(on))]))


def retype(excludes: Sequence[str], typed: Sequence[str]) -> tuple[str, ...]:
    """``excludes`` with its typed globs replaced by ``typed``; the presets stay put."""
    on, _old = split_presets(excludes)
    shipped = set(_globs_of(on))
    return tuple(dict.fromkeys([*(g for g in excludes if g in shipped), *typed]))
