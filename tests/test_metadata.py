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
        "version": 1,
        "sessions": {"codex:one": {"name": "feature-a"}},
    }

    assert store.remove("codex:one") is True
    assert store.read_names() == {}
    assert store.remove("codex:one") is False


def test_name_is_unique_across_provider_keys(tmp_path):
    store = MetadataStore(tmp_path)
    store.set_name("codex:one", "shared")

    with pytest.raises(MetadataError, match="已由"):
        store.set_name("claude:two", "shared")


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
