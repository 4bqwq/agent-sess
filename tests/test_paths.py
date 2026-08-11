from __future__ import annotations

import sess.paths as paths_module
from sess.paths import canonical_path, is_within, same_directory


def test_canonical_path_resolves_dot_segments(tmp_path):
    child = tmp_path / "child"
    child.mkdir()

    assert canonical_path(tmp_path / "child" / ".." / "child") == canonical_path(child)


def test_same_directory_uses_filesystem_identity_for_different_spellings(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_samefile(left, right):
        calls.append((left, right))
        return True

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    assert same_directory("/mnt/c/Users/Project", "/mnt/c/users/project")
    assert calls


def test_same_directory_rejects_an_actual_child(tmp_path):
    child = tmp_path / "child"
    child.mkdir()

    assert not same_directory(tmp_path, child)


def test_is_within_does_not_accept_sibling_prefix(tmp_path):
    root = tmp_path / "root"
    sibling = tmp_path / "root-other"
    root.mkdir()
    sibling.mkdir()

    assert is_within(root / "item", root)
    assert not is_within(sibling / "item", root)
