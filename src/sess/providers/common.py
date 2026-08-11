from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sess.errors import ProviderError


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_full_uuid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def iter_jsonl_snapshot(
    path: Path, warnings: list[str], provider_label: str
) -> Iterator[dict[str, object]]:
    """Yield JSON objects from the file size captured when reading starts."""

    try:
        with path.open("rb") as handle:
            remaining = os.fstat(handle.fileno()).st_size
            line_number = 0
            while remaining > 0:
                raw_line = handle.readline(remaining)
                if not raw_line:
                    warnings.append(
                        f"{provider_label} transcript 在读取快照时被截断: {path}"
                    )
                    break
                remaining -= len(raw_line)
                line_number += 1
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    warnings.append(
                        f"跳过 {provider_label} transcript 的无效 UTF-8 行 "
                        f"{path}:{line_number}: {exc}"
                    )
                    continue
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    if remaining == 0 and not raw_line.endswith(b"\n"):
                        warnings.append(
                            f"忽略 {provider_label} transcript 尚未完整写入的末行 "
                            f"{path}:{line_number}"
                        )
                    else:
                        warnings.append(
                            f"跳过 {provider_label} transcript 的无效 JSON 行 "
                            f"{path}:{line_number}: {exc.msg}"
                        )
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError as exc:
        raise ProviderError(f"无法读取 {provider_label} transcript {path}: {exc}") from exc


def format_tool_input(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value, "text"
        if not isinstance(parsed, (dict, list)):
            return value, "text"
        value = parsed
    return json.dumps(value, ensure_ascii=False, indent=2), "json"


def content_as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                content = item.get("content")
                if isinstance(content, (str, list, dict)):
                    parts.append(content_as_text(content))
                    continue
            parts.append(json.dumps(item, ensure_ascii=False, indent=2))
        return "\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False, indent=2)
