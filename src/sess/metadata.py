from __future__ import annotations

import json
import os
import tempfile
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
    VERSION = 1

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir.resolve(strict=False)
        self.path = self.project_dir / ".agent" / "sess_metadata.json"

    def read_names(self) -> dict[str, str]:
        if not self.path.exists():
            return {}

        try:
            with self.path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise MetadataError(f"无法读取 {self.path}: {exc}") from exc

        if not isinstance(document, dict):
            raise MetadataError(f"{self.path} 的顶层必须是 JSON object")
        if document.get("version") != self.VERSION:
            raise MetadataError(
                f"{self.path} 使用了不支持的 version: {document.get('version')!r}"
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
        return names

    def set_name(self, key: str, name: str) -> None:
        validate_name(name)
        names = self.read_names()
        for existing_key, existing_name in names.items():
            if existing_name == name and existing_key != key:
                raise MetadataError(
                    f"名称 {name!r} 已由 {existing_key!r} 使用，请选择其他名称"
                )
        names[key] = name
        self._write_names(names)

    def remove(self, key: str) -> bool:
        names = self.read_names()
        if key not in names:
            return False
        del names[key]
        self._write_names(names)
        return True

    def _write_names(self, names: dict[str, str]) -> None:
        document = {
            "version": self.VERSION,
            "sessions": {
                key: {"name": value} for key, value in sorted(names.items())
            },
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
