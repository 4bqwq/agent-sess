from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from sess.errors import ProviderError
from sess.models import (
    Agent,
    DeletionPreview,
    DeletionResult,
    DiscoveryResult,
    Session,
    SessionTrace,
    TraceEvent,
    TraceEventKind,
)
from sess.paths import same_directory
from sess.process import NativeProcessRunner
from sess.providers.base import SessionProvider
from sess.providers.common import (
    content_as_text,
    format_tool_input,
    is_full_uuid,
    iter_jsonl_snapshot,
    parse_timestamp,
)


class CodexProvider(SessionProvider):
    agent = Agent.CODEX
    _CONTROL_MESSAGE_PREFIXES = (
        "# AGENTS.md instructions for ",
        "<environment_context>",
        "<skill>",
        "<permissions instructions>",
        "<apps_instructions>",
        "<plugins_instructions>",
    )

    def __init__(self, codex_home: Path | None = None, executable: str = "codex") -> None:
        configured_home = os.environ.get("CODEX_HOME")
        self.codex_home = (
            codex_home
            if codex_home is not None
            else Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".codex"
        )
        self.executable = executable

    def discover(self, current_dir: Path) -> DiscoveryResult:
        sessions_root = self.codex_home / "sessions"
        if not sessions_root.exists():
            return DiscoveryResult()

        sessions: list[Session] = []
        warnings: list[str] = []
        try:
            transcript_paths = sorted(sessions_root.rglob("*.jsonl"))
        except OSError as exc:
            return DiscoveryResult(warnings=(f"无法扫描 Codex 会话目录 {sessions_root}: {exc}",))

        for transcript_path in transcript_paths:
            session, file_warnings = self._read_transcript(transcript_path, current_dir)
            warnings.extend(file_warnings)
            if session is not None:
                sessions.append(session)
        return DiscoveryResult(tuple(sessions), tuple(warnings))

    def _read_transcript(
        self, transcript_path: Path, target_cwd: Path
    ) -> tuple[Session | None, list[str]]:
        warnings: list[str] = []
        session_id: str | None = None
        cwd: str | None = None
        created_at = None
        updated_at = None
        matched = False

        try:
            with transcript_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        warnings.append(
                            f"跳过 Codex transcript 的无效 JSON 行 "
                            f"{transcript_path}:{line_number}: {exc.msg}"
                        )
                        continue
                    if not isinstance(event, dict):
                        continue

                    timestamp = parse_timestamp(event.get("timestamp"))
                    if timestamp is not None and (updated_at is None or timestamp > updated_at):
                        updated_at = timestamp

                    if event.get("type") != "session_meta":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        break
                    if payload.get("originator") != "codex-tui" or payload.get("source") != "cli":
                        break
                    raw_id = payload.get("id")
                    raw_cwd = payload.get("cwd")
                    if not isinstance(raw_id, str) or not isinstance(raw_cwd, str):
                        warnings.append(f"Codex session_meta 缺少 id 或 cwd: {transcript_path}")
                        break
                    if not is_full_uuid(raw_id):
                        warnings.append(f"Codex session_meta 包含无效 session ID: {transcript_path}")
                        break
                    if not same_directory(raw_cwd, target_cwd):
                        break
                    if timestamp is None:
                        warnings.append(f"Codex session_meta 缺少有效 timestamp: {transcript_path}")
                        break
                    session_id = raw_id
                    cwd = raw_cwd
                    created_at = timestamp
                    matched = True
        except (OSError, UnicodeError) as exc:
            warnings.append(f"无法读取 Codex transcript {transcript_path}: {exc}")
            return None, warnings

        if not matched or session_id is None or cwd is None or created_at is None:
            return None, warnings
        if updated_at is None:
            updated_at = created_at
        return (
            Session(
                agent=self.agent,
                session_id=session_id,
                cwd=Path(cwd),
                created_at=created_at,
                updated_at=updated_at,
                transcript_path=transcript_path,
            ),
            warnings,
        )

    def resume_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        return [self.executable, "resume", session_id, *native_args]

    def fork_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        return [self.executable, "fork", session_id, *native_args]

    def read_trace(self, session: Session, all_turns: bool) -> SessionTrace:
        if session.agent is not self.agent:
            raise ProviderError("CodexProvider 收到了其他 Agent 的会话")

        events: list[TraceEvent] = []
        warnings: list[str] = []
        tool_names: dict[str, str] = {}
        has_task_boundaries = False

        for event in iter_jsonl_snapshot(
            session.transcript_path, warnings, "Codex"
        ):
            timestamp = parse_timestamp(event.get("timestamp"))
            event_type = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            if event_type == "event_msg":
                message_type = payload.get("type")
                if message_type == "task_started":
                    has_task_boundaries = True
                    if not all_turns:
                        events.clear()
                        tool_names.clear()
                    continue
                if message_type in {"error", "stream_error"}:
                    error_value = payload.get("message", payload.get("error"))
                    if error_value is not None:
                        events.append(
                            TraceEvent(
                                kind=TraceEventKind.ERROR,
                                content=content_as_text(error_value),
                                timestamp=timestamp,
                            )
                        )
                elif message_type == "task_complete":
                    error_value = payload.get("error")
                    if error_value is not None:
                        events.append(
                            TraceEvent(
                                kind=TraceEventKind.ERROR,
                                content=content_as_text(error_value),
                                timestamp=timestamp,
                            )
                        )
                continue

            if event_type != "response_item":
                continue
            item_type = payload.get("type")
            if item_type == "message":
                role = payload.get("role")
                texts = self._message_texts(payload.get("content"))
                if role == "user":
                    texts = [
                        text for text in texts if not self._is_control_message(text)
                    ]
                    if not texts:
                        continue
                    if not all_turns and not has_task_boundaries:
                        events.clear()
                        tool_names.clear()
                    events.append(
                        TraceEvent(
                            kind=TraceEventKind.USER,
                            content="\n\n".join(texts),
                            timestamp=timestamp,
                        )
                    )
                elif role == "assistant" and texts:
                    phase = payload.get("phase")
                    label = (
                        "Commentary"
                        if phase == "commentary"
                        else "Final"
                        if phase == "final_answer"
                        else None
                    )
                    events.append(
                        TraceEvent(
                            kind=TraceEventKind.ASSISTANT,
                            content="\n\n".join(texts),
                            timestamp=timestamp,
                            label=label,
                        )
                    )
                continue

            if item_type == "reasoning":
                summary = payload.get("summary")
                if isinstance(summary, list):
                    parts: list[str] = []
                    for part in summary:
                        if isinstance(part, str) and part:
                            parts.append(part)
                        elif isinstance(part, dict):
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                parts.append(text)
                    if parts:
                        events.append(
                            TraceEvent(
                                kind=TraceEventKind.REASONING,
                                content="\n\n".join(parts),
                                timestamp=timestamp,
                            )
                        )
                continue

            if item_type in {"custom_tool_call", "function_call"}:
                tool_name = payload.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    tool_name = "unknown"
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    tool_names[call_id] = tool_name
                raw_input = (
                    payload.get("input")
                    if item_type == "custom_tool_call"
                    else payload.get("arguments")
                )
                content, language = format_tool_input(raw_input)
                events.append(
                    TraceEvent(
                        kind=TraceEventKind.TOOL_CALL,
                        content=content,
                        timestamp=timestamp,
                        label=tool_name,
                        language=language,
                    )
                )
                continue

            if item_type in {"custom_tool_call_output", "function_call_output"}:
                call_id = payload.get("call_id")
                tool_name = (
                    tool_names.get(call_id, call_id)
                    if isinstance(call_id, str)
                    else None
                )
                output = content_as_text(payload.get("output"))
                events.append(
                    TraceEvent(
                        kind=TraceEventKind.TOOL_RESULT,
                        content=output,
                        timestamp=timestamp,
                        label=tool_name,
                        language="text",
                    )
                )

        return SessionTrace(
            session=session,
            events=tuple(events),
            all_turns=all_turns,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _message_texts(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        texts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in {"input_text", "output_text", "text"}:
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        return texts

    @classmethod
    def _is_control_message(cls, text: str) -> bool:
        stripped = text.lstrip()
        return any(
            stripped.startswith(prefix) for prefix in cls._CONTROL_MESSAGE_PREFIXES
        )

    def preview_delete(self, session: Session) -> DeletionPreview:
        return DeletionPreview(
            (
                str(session.transcript_path),
                f"Codex 为 session {session.session_id} 维护的原生索引和关联状态",
            )
        )

    def delete(
        self, session: Session, runner: NativeProcessRunner
    ) -> DeletionResult:
        return_code = runner.run(
            [self.executable, "delete", "--force", session.session_id]
        )
        if return_code != 0:
            return DeletionResult(
                remaining=(f"Codex session {session.session_id}（退出码 {return_code}）",)
            )
        if session.transcript_path.exists():
            return DeletionResult(remaining=(str(session.transcript_path),))
        return DeletionResult(deleted=(f"Codex session {session.session_id}",))
