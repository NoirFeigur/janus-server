"""Unit tests for the subtree-walk helper (``_collect_subtree``).

A pure synchronous helper, kept in its own module (no ``pytest.mark.asyncio``
module mark) so the sync tests aren't spuriously flagged as mismarked async
tests. Pins the adjacency-list walk's visited guard: a node reachable by two
paths (or a root passed twice) must be collected exactly once.
"""

from __future__ import annotations

from src.auth.service import _collect_subtree


def test_collect_subtree_dedups_repeated_roots() -> None:
    """A root id appearing twice must be collected once (the visited guard)."""
    # 1 -> 2 ; passing root 1 twice forces the already-collected branch.
    pairs = [(2, 1)]
    assert _collect_subtree([1, 1], pairs) == {1, 2}


def test_collect_subtree_dedups_via_shared_descendant() -> None:
    """Two roots sharing a descendant collect it once (diamond adjacency)."""
    # roots 1 and 2 both parent 3 ; 3 must not be double-visited.
    pairs = [(3, 1), (3, 2)]
    assert _collect_subtree([1, 2], pairs) == {1, 2, 3}


def test_collect_subtree_single_root_no_children() -> None:
    """A root with no descendants collects just itself."""
    assert _collect_subtree([5], []) == {5}
