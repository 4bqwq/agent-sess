from __future__ import annotations

from pathlib import Path

import pytest

import sess.paths as paths_module
from sess.paths import (
    canonical_path,
    directory_scan_cache,
    is_within,
    same_directory,
)


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


def test_scan_cache_reuses_path_resolution_for_path_and_string(tmp_path, monkeypatch):
    path = tmp_path / "project"
    resolved_paths: list[Path] = []

    def fake_resolve(candidate, *, strict=False):
        resolved_paths.append(candidate)
        assert strict is False
        return candidate

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    with directory_scan_cache():
        assert canonical_path(path) == canonical_path(str(path))
        assert canonical_path(path) == str(path)
        assert resolved_paths == [path]

    canonical_path(path)
    canonical_path(str(path))
    assert resolved_paths == [path, path, path]


@pytest.mark.parametrize("samefile_result", [True, False])
def test_scan_cache_reuses_true_and_false_directory_results(
    tmp_path, monkeypatch, samefile_result
):
    left = tmp_path / "left"
    right = tmp_path / "right"
    calls: list[tuple[str, str]] = []

    def fake_samefile(left_path, right_path):
        calls.append((left_path, right_path))
        return samefile_result

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    with directory_scan_cache():
        assert same_directory(left, right) is samefile_result
        assert same_directory(str(left), str(right)) is samefile_result
        assert same_directory(left, str(right)) is samefile_result
        assert len(calls) == 1

    assert same_directory(left, right) is samefile_result
    assert len(calls) == 2


def test_scan_cache_reuses_target_resolution_across_different_directories(
    tmp_path, monkeypatch
):
    left = tmp_path / "left"
    other = tmp_path / "other"
    target = tmp_path / "target"
    resolved_paths: list[Path] = []
    compared_paths: list[tuple[str, str]] = []

    def fake_resolve(candidate, *, strict=False):
        resolved_paths.append(candidate)
        return candidate

    def fake_samefile(left_path, right_path):
        compared_paths.append((left_path, right_path))
        return False

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    with directory_scan_cache():
        assert not same_directory(left, target)
        assert not same_directory(other, str(target))
        assert not same_directory(str(left), target)

    assert resolved_paths == [left, target, other]
    assert compared_paths == [(str(left), str(target)), (str(other), str(target))]


def test_scan_cache_still_uses_filesystem_identity_for_different_spellings(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    alias = tmp_path / "project-alias"
    calls: list[tuple[str, str]] = []

    def fake_samefile(left, right):
        calls.append((left, right))
        return True

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    with directory_scan_cache():
        assert same_directory(project, alias)
        assert same_directory(str(project), str(alias))

    assert calls == [(canonical_path(project), canonical_path(alias))]


def test_scan_cache_keeps_equal_paths_short_circuit(tmp_path, monkeypatch):
    project = tmp_path / "project"

    def unexpected_samefile(left, right):
        pytest.fail("equal canonical paths must not call samefile")

    monkeypatch.setattr(paths_module.os.path, "samefile", unexpected_samefile)

    with directory_scan_cache():
        assert same_directory(project, str(project))
        assert same_directory(project, project)


def test_scan_cache_preserves_dot_segments_and_excludes_child_and_sibling(tmp_path):
    project = tmp_path / "project"
    child = project / "child"
    sibling = tmp_path / "project-other"
    child.mkdir(parents=True)
    sibling.mkdir()

    with directory_scan_cache():
        assert same_directory(project, child / "..")
        assert same_directory(child, project / "." / "child")
        assert not same_directory(project, child)
        assert not same_directory(project, sibling)


def test_scan_cache_rechecks_changed_symlink_in_next_scope(tmp_path):
    project = tmp_path / "project"
    other = tmp_path / "other"
    alias = tmp_path / "alias"
    project.mkdir()
    other.mkdir()
    alias.symlink_to(project, target_is_directory=True)

    with directory_scan_cache():
        assert same_directory(alias, project)
        assert not same_directory(alias, other)

    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)

    with directory_scan_cache():
        assert not same_directory(alias, project)
        assert same_directory(alias, other)


def test_scan_cache_rechecks_samefile_in_next_scope(tmp_path, monkeypatch):
    project = tmp_path / "project"
    alias = tmp_path / "alias"
    calls: list[tuple[str, str]] = []
    results = iter([True, False])

    def fake_samefile(left, right):
        calls.append((left, right))
        return next(results)

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    with directory_scan_cache():
        assert same_directory(project, alias)
        assert same_directory(project, alias)

    with directory_scan_cache():
        assert not same_directory(project, alias)
        assert not same_directory(project, alias)

    assert len(calls) == 2


def test_scan_cache_keeps_samefile_oserror_result(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []

    def missing_samefile(left, right):
        calls.append((left, right))
        raise FileNotFoundError("directory disappeared")

    monkeypatch.setattr(paths_module.os.path, "samefile", missing_samefile)

    with directory_scan_cache():
        assert not same_directory(tmp_path / "left", tmp_path / "right")
        assert not same_directory(tmp_path / "left", tmp_path / "right")

    assert len(calls) == 1


def test_scan_cache_resets_after_exception(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []
    results = iter([True, False])

    def fake_samefile(left, right):
        calls.append((left, right))
        return next(results)

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    with pytest.raises(RuntimeError, match="scan failed"):
        with directory_scan_cache():
            assert same_directory(tmp_path / "project", tmp_path / "alias")
            raise RuntimeError("scan failed")

    assert not same_directory(tmp_path / "project", tmp_path / "alias")
    assert len(calls) == 2


@pytest.mark.parametrize("inner_raises", [False, True])
def test_nested_scan_cache_restores_outer_scope(tmp_path, monkeypatch, inner_raises):
    project = tmp_path / "project"
    alias = tmp_path / "alias"
    calls: list[tuple[str, str]] = []
    results = iter([True, False, False])

    def fake_samefile(left, right):
        calls.append((left, right))
        return next(results)

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    with directory_scan_cache():
        assert same_directory(project, alias)
        if inner_raises:
            with pytest.raises(RuntimeError, match="inner failed"):
                with directory_scan_cache():
                    assert not same_directory(project, alias)
                    raise RuntimeError("inner failed")
        else:
            with directory_scan_cache():
                assert not same_directory(project, alias)
                assert not same_directory(project, alias)

        assert same_directory(project, alias)
        assert len(calls) == 2

    assert not same_directory(project, alias)
    assert len(calls) == 3
