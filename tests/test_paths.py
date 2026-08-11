from __future__ import annotations

from sess.paths import canonical_path, is_within


def test_canonical_path_resolves_dot_segments(tmp_path):
    child = tmp_path / "child"
    child.mkdir()

    assert canonical_path(tmp_path / "child" / ".." / "child") == canonical_path(child)


def test_is_within_does_not_accept_sibling_prefix(tmp_path):
    root = tmp_path / "root"
    sibling = tmp_path / "root-other"
    root.mkdir()
    sibling.mkdir()

    assert is_within(root / "item", root)
    assert not is_within(sibling / "item", root)

