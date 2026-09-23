"""Result rows that share a basename are told apart by their folder.

Rows carry the basename, so `build/out-01.md` and `notes/out-01.md` would be two
identical rows, and "which ones did that drop?" could not be answered from the
result list.
"""

from __future__ import annotations

from collections.abc import Callable

from fnd.tui.results_labels import disambiguated_names


def test_a_unique_name_stays_a_bare_name() -> None:
    names = disambiguated_names(["/vault/notes/alpha.md", "/vault/build/beta.md"])

    assert names["/vault/notes/alpha.md"] == "alpha.md"
    assert names["/vault/build/beta.md"] == "beta.md"


def test_a_shared_name_gains_its_folder() -> None:
    names = disambiguated_names(["/vault/build/out-01.md", "/vault/notes/out-01.md"])

    assert names["/vault/build/out-01.md"] == "build/out-01.md"
    assert names["/vault/notes/out-01.md"] == "notes/out-01.md"


def test_it_keeps_going_until_they_differ() -> None:
    """One folder is not always enough."""
    names = disambiguated_names(["/a/x/gen/out.md", "/b/y/gen/out.md"])

    assert names["/a/x/gen/out.md"] == "x/gen/out.md"
    assert names["/b/y/gen/out.md"] == "y/gen/out.md"


def test_three_of_a_name_all_qualify() -> None:
    names = disambiguated_names(["/v/a/n.md", "/v/b/n.md", "/v/c/n.md"])

    assert set(names.values()) == {"a/n.md", "b/n.md", "c/n.md"}


def test_only_the_shared_names_grow() -> None:
    """The control: qualifying every row would cost width for nothing."""
    names = disambiguated_names(["/v/a/n.md", "/v/b/n.md", "/v/deep/other.md"])

    assert names["/v/deep/other.md"] == "other.md"


def test_the_same_path_twice_is_not_its_own_rival() -> None:
    """One file listed once: a set of one cannot need qualifying."""
    names = disambiguated_names(["/v/a/n.md"])

    assert names["/v/a/n.md"] == "n.md"


def test_it_does_not_grow_quadratically_with_the_result_count() -> None:
    """`_refresh_status` reaches this from twenty call sites, so a focus change
    pays it. Measured quadratic: 4.9 ms at 50 rows, 73.5 ms at 200.

    Asserted as a ratio rather than a wall-clock budget: the shape is what
    matters, and a threshold in milliseconds would be a flake on a busy box.
    """
    import time

    from fnd.tui.results_labels import disambiguated_names

    def paths(n: int) -> list[str]:
        return [f"/vault/area{i // 5}/sub{i % 5}/index.md" for i in range(n)]

    def best_of(n: int) -> float:
        ps = paths(n)
        return min(_timed(disambiguated_names, ps) for _ in range(5))

    def _timed(fn: Callable[[list[str]], object], arg: list[str]) -> float:
        t0 = time.perf_counter()
        fn(arg)
        return time.perf_counter() - t0

    small, large = best_of(50), best_of(200)

    # Quadratic would be ~16x for a 4x input; the grouped form is near-linear.
    assert large < small * 8, f"50 rows {small * 1000:.1f} ms, 200 rows {large * 1000:.1f} ms"
