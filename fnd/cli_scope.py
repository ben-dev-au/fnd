"""Collect unrecognised CLI filter values, then report them all at once.

A command can name several filters, so failing on the first bad one costs the
user a re-run per typo. :class:`FilterIssues` records each problem instead of
raising, and :func:`resolve_or_exit` applies one policy over the whole batch:
offer a single confirmation when every problem has an obvious fix, otherwise
print them all and exit.

The rule that makes this safe: :meth:`FilterIssues.resolve` hands back the
correction it would apply, and :func:`resolve_or_exit` only lets a command
proceed when every recorded problem was correctable *and* confirmed. So a
caller can use the returned values directly — by the time it runs, they're
either the user's own or ones the user accepted.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import typer

from fnd.config import is_all_collections
from fnd.vocabulary import collection_vocabulary

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fnd.config import Config
    from fnd.query_errors import UnknownFilterValueError
    from fnd.vocabulary import Vocabulary

__all__ = [
    "FilterIssues",
    "resolve_collection_option",
    "resolve_launch_collection",
    "resolve_or_exit",
]

# Enough known values to orient the user without filling the terminal.
_MAX_LISTED = 12


class FilterIssues:
    """Every unrecognised filter value on one command.

    Checks record and continue rather than raising, so one pass can validate
    the whole command line before the user sees anything.
    """

    def __init__(self) -> None:
        self._issues: list[UnknownFilterValueError] = []

    def __bool__(self) -> bool:
        return bool(self._issues)

    @property
    def issues(self) -> tuple[UnknownFilterValueError, ...]:
        return tuple(self._issues)

    def resolve(self, vocab: Vocabulary, raw: str, *, flag: str | None = None) -> str:
        """Canonical spelling of ``raw``, recording it if it isn't legal.

        Returns the single suggestion when there is one, so the caller carries
        on with the value the user is about to be offered.
        """
        hit = vocab.match(raw)
        if hit is not None:
            return hit
        err = vocab.unknown(raw, flag=flag)
        self._issues.append(err)
        return err.correction or raw

    def check(self, vocab: Vocabulary, raw: str, *, flag: str | None = None) -> None:
        """Record ``raw`` if it isn't legal, offering no correction — for
        values the CLI reports but won't rewrite on the user's behalf."""
        if vocab.match(raw) is None:
            self._issues.append(vocab.unknown(raw, flag=flag))

    def resolve_each(
        self, vocab: Vocabulary, values: Iterable[str], *, flag: str | None = None
    ) -> list[str]:
        """:meth:`resolve` over a repeatable flag's values, order preserved."""
        return [self.resolve(vocab, v, flag=flag) for v in values]

    def split_resolve(self, vocab: Vocabulary, raw: str, *, flag: str | None = None) -> list[str]:
        """Canonical names from a comma-separated value, recording unknowns."""
        known, unknown = vocab.split_resolve(raw)
        for value in unknown:
            err = vocab.unknown(value, flag=flag)
            self._issues.append(err)
            if err.correction is not None:
                known.append(err.correction)
        if not known and not unknown and raw.strip():
            # Punctuation-only input ("," / ", ,") — resolves to nothing but
            # must not quietly widen the search to every collection.
            self._issues.append(vocab.unknown(raw, flag=flag))
        return list(dict.fromkeys(known))


def resolve_collection_option(
    raw: str | None,
    config: Config,
    issues: FilterIssues,
    *,
    flag: str = "--collection",
) -> list[str] | None:
    """Resolve a ``--collection`` value to canonical names for the searcher.

    None means "every collection" — both the unset flag and the ``all``
    pseudo-name, which is checked before the vocabulary so a collection
    literally named ``all`` still wins.

    A config with no collections is passed through unvalidated: there's
    nothing to compare against, and an ad-hoc ``fnd index --collection`` name
    is then indistinguishable from a typo.
    """
    if not raw or is_all_collections(raw, known=set(config.collections)):
        return None
    if not config.collections:
        return [raw]
    return issues.split_resolve(collection_vocabulary(config), raw, flag=flag) or None


def resolve_launch_collection(
    raw: str | None,
    config: Config,
    issues: FilterIssues,
    *,
    flag: str = "--collection",
) -> str | None:
    """The same value canonicalised for the TUI, which takes one string.

    ``all`` passes through untouched: the sidebar expands it against the live
    config, and collapsing it to None here would read as "no flag given" and
    let a saved scope win instead.
    """
    if not raw or is_all_collections(raw, known=set(config.collections)):
        return raw or None
    if not config.collections:
        return raw
    names = issues.split_resolve(collection_vocabulary(config), raw, flag=flag)
    return ",".join(names) if names else None


def resolve_or_exit(issues: FilterIssues, *, is_tty: bool | None = None) -> None:
    """Report every recorded problem, offering a fix when there is one.

    Returns normally only when nothing was recorded, or when every problem had
    a single fix on a flag and the user accepted them. ``is_tty`` is exposed
    for tests; it defaults to ``sys.stdin.isatty()``.
    """
    problems = issues.issues
    if not problems:
        return

    # A query token is reported but never rewritten: silently editing text the
    # user typed is more surprising than correcting a flag, and the message
    # already names the exact edit. One unfixable problem also cancels the
    # offer for the rest — accepting would still leave a broken command.
    fixable = [e for e in problems if e.flag is not None and e.correction is not None]
    all_fixable = len(fixable) == len(problems)
    if not all_fixable or not _interactive(is_tty):
        for err in problems:
            typer.echo(_describe(err), err=True)
        if all_fixable:
            retry = " ".join(f"{e.flag} {e.correction}" for e in fixable)
            typer.echo(f"Re-run with: {retry}", err=True)
        raise typer.Exit(code=2)

    if len(problems) == 1:
        err = problems[0]
        typer.echo(f"{err.message}.", err=True)
        prompt = f"Did you mean {err.correction!r}?"
    else:
        typer.echo(f"{len(problems)} filter values weren't recognised:", err=True)
        width = max(len(f"{e.flag} {e.value}") for e in problems)
        for err in problems:
            typer.echo(f"  {f'{err.flag} {err.value}':<{width}}  → {err.correction!r}", err=True)
        prompt = "Use these instead?"

    if not typer.confirm(prompt, default=True):
        typer.echo("aborted", err=True)
        raise typer.Exit(code=2)


def _describe(err: UnknownFilterValueError) -> str:
    """One line naming the problem, and the way out of it if there is one."""
    head = f"{err.flag}: {err.message}" if err.flag else err.message
    if err.hint:
        return f"{head} — {err.hint}"
    if err.known:
        listed = ", ".join(err.known[:_MAX_LISTED])
        more = "" if len(err.known) <= _MAX_LISTED else f", … ({len(err.known)} total)"
        return f"{head}. Known {err.label}s: {listed}{more}"
    return f"{head}. None are configured."


def _interactive(is_tty: bool | None) -> bool:
    if is_tty is not None:
        return is_tty
    # Allow tests to force the TTY path — same escape hatch as fnd.migrate.
    return True if os.environ.get("_FND_FORCE_TTY") == "1" else sys.stdin.isatty()
