from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sess.errors import MetadataError


def validate_name(name: str) -> None:
    if not name:
        raise MetadataError("名称不能为空")
    if name != name.strip():
        raise MetadataError("名称不能以空白字符开头或结尾")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise MetadataError("名称不能包含控制字符")


class MetadataStore:
    VERSION = 2

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.resolve(strict=False)
        self.path = self.project_dir / ".agent" / "sess_metadata.json"

    def read_names(self) -> dict[str, str]:
        return dict(self._read_document().names)

    def _read_document(self) -> _MetadataDocument:
        if not self.path.exists():
            return _MetadataDocument({}, {})

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise MetadataError(f"无法读取 {self.path}: {exc}") from exc

        if not isinstance(document, dict):
            raise MetadataError(f"{self.path} 的顶层必须是 JSON object")
        version = document.get("version")
        if version not in {1, self.VERSION}:
            raise MetadataError(
                f"{self.path} 使用了不支持的 version: {version!r}"
            )

        sessions = document.get("sessions")
        if not isinstance(sessions, dict):
            raise MetadataError(f"{self.path} 的 sessions 必须是 JSON object")

        names: dict[str, str] = {}
        for key, value in sessions.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise MetadataError(f"{self.path} 包含无效的 session 元数据项")
            name = value.get("name")
            if not isinstance(name, str) or not name:
                raise MetadataError(f"{self.path} 中 {key!r} 的 name 必须是非空字符串")
            try:
                validate_name(name)
            except MetadataError as exc:
                raise MetadataError(f"{self.path} 中 {key!r} 的 name 无效: {exc}") from exc
            names[key] = name

        fork_counters: dict[str, int] = {}
        if version == self.VERSION:
            raw_counters = document.get("fork_counters")
            if not isinstance(raw_counters, dict):
                raise MetadataError(
                    f"{self.path} 的 fork_counters 必须是 JSON object"
                )
            for key, value in raw_counters.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    raise MetadataError(
                        f"{self.path} 包含无效的 fork 计数器"
                    )
                fork_counters[key] = value
        return _MetadataDocument(names, fork_counters)

    def set_name(self, key: str, name: str) -> None:
        validate_name(name)
        document = self._read_document()
        self._ensure_name_available(document.names, name, key)
        document.names[key] = name
        self._write_document(document)

    def ensure_name_available(self, name: str) -> None:
        validate_name(name)
        self._ensure_name_available(self._read_document().names, name)

    def assign_fork_name(
        self,
        parent_key: str,
        child_key: str,
        parent_prefix: str,
    ) -> str:
        document = self._read_document()
        existing_name = document.names.get(child_key)
        if existing_name is not None:
            return existing_name

        used_names = set(document.names.values())
        number = document.fork_counters.get(parent_key, 0)
        while True:
            number += 1
            name = f"{parent_prefix}-fork{number}"
            if name not in used_names:
                break

        validate_name(name)
        document.names[child_key] = name
        document.fork_counters[parent_key] = number
        self._write_document(document)
        return name

    def remove(self, key: str) -> bool:
        document = self._read_document()
        removed_name = document.names.pop(key, None)
        removed_counter = document.fork_counters.pop(key, None)
        if removed_name is None and removed_counter is None:
            return False
        self._write_document(document)
        return removed_name is not None

    def _write_document(self, metadata: _MetadataDocument) -> None:
        document = {
            "version": self.VERSION,
            "sessions": {
                key: {"name": value}
                for key, value in sorted(metadata.names.items())
            },
            "fork_counters": dict(sorted(metadata.fork_counters.items())),
        }

        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".sess_metadata.", suffix=".tmp", dir=parent
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        except OSError as exc:
            raise MetadataError(f"无法写入 {self.path}: {exc}") from exc

    @staticmethod
    def _ensure_name_available(
        names: dict[str, str], name: str, key: str | None = None
    ) -> None:
        for existing_key, existing_name in names.items():
            if existing_name == name and existing_key != key:
                raise MetadataError(
                    f"名称 {name!r} 已由 {existing_key!r} 使用，请选择其他名称"
                )


@dataclass
class _MetadataDocument:
    names: dict[str, str]
    fork_counters: dict[str, int]
