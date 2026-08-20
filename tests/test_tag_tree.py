"""Flat tag list -> nested tree for the Tags pane."""

from __future__ import annotations

from fnd.tag_catalogue import TagCount, build_tag_tree


def test_flat_tags_become_roots() -> None:
    nodes = build_tag_tree([TagCount("recipe", 3), TagCount("travel", 1)])
    assert [(n.label, n.value, n.files, len(n.children)) for n in nodes] == [
        ("recipe", "recipe", 3, 0),
        ("travel", "travel", 1, 0),
    ]


def test_nested_tags_nest_under_their_parent() -> None:
    nodes = build_tag_tree(
        [
            TagCount("project", 3),
            TagCount("project/alpha", 2),
            TagCount("project/beta", 1),
        ]
    )
    assert len(nodes) == 1
    parent = nodes[0]
    assert (parent.label, parent.value, parent.files) == ("project", "project", 3)
    assert [(c.label, c.value, c.files) for c in parent.children] == [
        ("alpha", "project/alpha", 2),
        ("beta", "project/beta", 1),
    ]


def test_label_is_the_leaf_segment_but_value_is_the_full_path() -> None:
    """The pane shows 'alpha'; the filter must use 'project/alpha'."""
    nodes = build_tag_tree([TagCount("a/b/c", 1)])
    a = nodes[0]
    b = a.children[0]
    c = b.children[0]
    assert (a.label, a.value) == ("a", "a")
    assert (b.label, b.value) == ("b", "a/b")
    assert (c.label, c.value) == ("c", "a/b/c")


def test_missing_ancestor_is_synthesised_with_zero_count() -> None:
    """Ancestors are normally expanded at index time, but a catalogue
    truncated by `limit` can drop one; the tree must not lose the child."""
    nodes = build_tag_tree([TagCount("orphan/child", 2)])
    assert len(nodes) == 1
    assert (nodes[0].label, nodes[0].files) == ("orphan", 0)
    assert nodes[0].children[0].value == "orphan/child"


def test_children_sorted_by_count_then_name() -> None:
    nodes = build_tag_tree(
        [
            TagCount("p", 9),
            TagCount("p/zebra", 5),
            TagCount("p/apple", 5),
            TagCount("p/mango", 7),
        ]
    )
    assert [c.label for c in nodes[0].children] == ["mango", "apple", "zebra"]


def test_roots_sorted_by_count_then_name() -> None:
    nodes = build_tag_tree([TagCount("b", 1), TagCount("a", 5), TagCount("c", 5)])
    assert [n.label for n in nodes] == ["a", "c", "b"]


def test_empty_input_yields_no_nodes() -> None:
    assert build_tag_tree([]) == []


def test_descendant_values_lists_the_whole_subtree() -> None:
    nodes = build_tag_tree(
        [TagCount("p", 1), TagCount("p/a", 1), TagCount("p/a/x", 1), TagCount("p/b", 1)]
    )
    assert nodes[0].descendant_values() == {"p", "p/a", "p/a/x", "p/b"}
