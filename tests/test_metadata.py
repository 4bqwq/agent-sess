from __future__ import annotations

import json

import pytest

from sess.errors import MetadataError
from sess.metadata import MetadataStore


def test_read_does_not_create_metadata_directory(tmp_path):
    store = MetadataStore(tmp_path)

    assert store.read_names() == {}
    assert not (tmp_path / ".agent").exists()


def test_set_and_remove_name_use_versioned_json(tmp_path):
    store = MetadataStore(tmp_path)

    store.set_name("codex:one", "feature-a")

    assert store.read_names() == {"codex:one": "feature-a"}
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document == {
        "version": 2,
        "sessions": {"codex:one": {"name": "feature-a"}},
        "fork_counters": {},
    }

    assert store.remove("codex:one") is True
    assert store.read_names() == {}
    assert store.remove("codex:one") is False


def test_name_is_unique_across_provider_keys(tmp_path):
    store = MetadataStore(tmp_path)
    store.set_name("codex:one", "shared")

    with pytest.raises(MetadataError, match="已由"):
        store.set_name("claude:two", "shared")

    with pytest.raises(MetadataError, match="已由"):
        store.ensure_name_available("shared")


def test_version_one_names_are_migrated_without_data_loss(tmp_path):
    path = tmp_path / ".agent" / "sess_metadata.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {"codex:one": {"name": "existing"}},
            }
        ),
        encoding="utf-8",
    )
    store = MetadataStore(tmp_path)

    store.set_name("claude:two", "new")

    assert store.read_names() == {
        "codex:one": "existing",
        "claude:two": "new",
    }
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_fork_numbers_are_scoped_to_parent_and_not_reused(tmp_path):
    store = MetadataStore(tmp_path)

    first = store.assign_fork_name("codex:parent", "codex:child-1", "019fec4f")
    assert store.remove("codex:child-1") is True
    second = store.assign_fork_name("codex:parent", "codex:child-2", "019fec4f")
    other_parent = store.assign_fork_name(
        "codex:other-parent", "codex:child-3", "019fed00"
    )

    assert first == "019fec4f-fork1"
    assert second == "019fec4f-fork2"
    assert other_parent == "019fed00-fork1"
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["fork_counters"] == {
        "codex:other-parent": 1,
        "codex:parent": 2,
    }


def test_invalid_metadata_is_reported(tmp_path):
    path = tmp_path / ".agent" / "sess_metadata.json"
    path.parent.mkdir()
    path.write_text('{"version": 9, "sessions": {}}', encoding="utf-8")

    with pytest.raises(MetadataError, match="不支持的 version"):
        MetadataStore(tmp_path).read_names()


def test_control_characters_are_rejected_in_names(tmp_path):
    store = MetadataStore(tmp_path)

    with pytest.raises(MetadataError, match="控制字符"):
        store.set_name("codex:one", "unsafe\x1bname")
