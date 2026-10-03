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
    ConversationTurn,
    Session,
    SessionConversation,
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
        try:
            transcript_paths = self._transcript_paths()
        except OSError as exc:
            return DiscoveryResult(
                warnings=(
                    f"无法扫描 Codex 会话目录 "
                    f"{self.codex_home / 'sessions'}: {exc}",
                )
            )
        return self._discover_paths(transcript_paths, current_dir)

    def snapshot_transcript_paths(self) -> frozenset[Path]:
        try:
            return frozenset(self._transcript_paths())
        except OSError as exc:
            raise ProviderError(
                f"无法扫描 Codex 会话目录 "
                f"{self.codex_home / 'sessions'}: {exc}"
            ) from exc

    def discover_new(
        self, current_dir: Path, known_paths: frozenset[Path]
    ) -> DiscoveryResult:
        try:
            transcript_paths = tuple(
                path
                for path in self._transcript_paths()
                if path not in known_paths
            )
        except OSError as exc:
            raise ProviderError(
                f"无法扫描 Codex 会话目录 "
                f"{self.codex_home / 'sessions'}: {exc}"
            ) from exc
        return self._discover_paths(
            transcript_paths, current_dir, current_dir_warnings_only=True
        )

    def _transcript_paths(self) -> tuple[Path, ...]:
        sessions_root = self.codex_home / "sessions"
        if not sessions_root.exists():
            return ()
        return tuple(sorted(sessions_root.rglob("*.jsonl")))

    def _discover_paths(
        self,
        transcript_paths: Sequence[Path],
        current_dir: Path,
        *,
        current_dir_warnings_only: bool = False,
    ) -> DiscoveryResult:
        sessions: list[Session] = []
        warnings: list[str] = []
        for transcript_path in transcript_paths:
            session, file_warnings = self._read_transcript(
                transcript_path,
                current_dir,
                current_dir_warnings_only=current_dir_warnings_only,
            )
            warnings.extend(file_warnings)
            if session is not None:
                sessions.append(session)
        return DiscoveryResult(tuple(sessions), tuple(warnings))

    def _read_transcript(
        self,
        transcript_path: Path,
        target_cwd: Path,
        *,
        current_dir_warnings_only: bool = False,
    ) -> tuple[Session | None, list[str]]:
        warnings: list[str] = []
        session_id: str | None = None
        cwd: str | None = None
        created_at = None
        updated_at = None
        matched = False
        belongs_to_current_dir = False
        forked_from_id: str | None = None

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
                    if matched:
                        # Fork rollouts copy the parent transcript after the
                        # child's own metadata. Those copied records must not
                        # replace the identity of the rollout being scanned.
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        break
                    if payload.get("originator") != "codex-tui":
                        break
                    source = payload.get("source")
                    if source in ("exec", "subagent") or (
                        isinstance(source, dict) and "subagent" in source
                    ):
                        break
                    raw_cwd = payload.get("cwd")
                    if not isinstance(raw_cwd, str):
                        warnings.append(f"Codex session_meta 缺少 id 或 cwd: {transcript_path}")
                        break
                    belongs_to_current_dir = same_directory(raw_cwd, target_cwd)
                    if source not in ("cli", "vscode"):
                        if belongs_to_current_dir:
                            warnings.append(
                                f"Codex session_meta 包含不支持的 source {source!r}: "
                                f"{transcript_path}"
                            )
                        break
                    raw_id = payload.get("id")
                    if not isinstance(raw_id, str):
                        warnings.append(f"Codex session_meta 缺少 id 或 cwd: {transcript_path}")
                        break
                    if not is_full_uuid(raw_id):
                        warnings.append(f"Codex session_meta 包含无效 session ID: {transcript_path}")
                        break
                    if not belongs_to_current_dir:
                        break
                    if timestamp is None:
                        warnings.append(f"Codex session_meta 缺少有效 timestamp: {transcript_path}")
                        break
                    session_id = raw_id
                    cwd = raw_cwd
                    created_at = timestamp
                    raw_parent_id = payload.get(
                        "forked_from_id"
                    ) or payload.get("parent_thread_id")
                    if raw_parent_id is not None:
                        if is_full_uuid(raw_parent_id):
                            forked_from_id = raw_parent_id
                        else:
                            warnings.append(
                                "Codex session_meta 包含无效的父 session ID: "
                                f"{transcript_path}"
                            )
                    matched = True
        except (OSError, UnicodeError) as exc:
            warnings.append(f"无法读取 Codex transcript {transcript_path}: {exc}")
            matched = False

        if current_dir_warnings_only and not belongs_to_current_dir:
            # Other projects and subagents may create files during this launch.
            # Their scan warnings are not evidence that this session failed.
            warnings.clear()
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
                forked_from_id=forked_from_id,
            ),
            warnings,
        )

    def resume_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        return [
            self.executable,
            "resume",
            session_id,
            *self._permission_defaults(native_args),
            *native_args,
        ]

    def new_argv(self, native_args: Sequence[str]) -> list[str]:
        return [self.executable, *self._permission_defaults(native_args), *native_args]

    def fork_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        return [
            self.executable,
            "fork",
            session_id,
            *self._permission_defaults(native_args),
            *native_args,
        ]

    @staticmethod
    def _permission_defaults(native_args: Sequence[str]) -> list[str]:
        sandbox_overridden = False
        approval_overridden = False
        arguments = iter(native_args)
        for argument in arguments:
            if argument == "--":
                break
            if argument in {
                "--dangerously-bypass-approvals-and-sandbox",
                "--yolo",
                "--full-auto",
                "--approve-for-me",
            }:
                # These native shortcuts select both permission settings.
                return []
            if argument in {"--sandbox", "-s"}:
                sandbox_overridden = True
                next(arguments, None)
            elif argument.startswith("--sandbox=") or (
                argument.startswith("-s") and not argument.startswith("--")
            ):
                sandbox_overridden = True
            elif argument in {"--ask-for-approval", "-a"}:
                approval_overridden = True
                next(arguments, None)
            elif argument.startswith("--ask-for-approval=") or (
                argument.startswith("-a") and not argument.startswith("--")
            ):
                approval_overridden = True
            elif argument in {"-c", "--config"} or argument.startswith(
                ("--config=", "-c")
            ):
                if argument in {"-c", "--config"}:
                    override = next(arguments, "")
                elif argument.startswith("--config="):
                    override = argument[len("--config=") :]
                else:
                    override = argument[2:].removeprefix("=")
                key, separator, _ = override.partition("=")
                if separator:
                    key = key.strip()
                    sandbox_overridden |= key == "sandbox_mode"
                    approval_overridden |= (
                        key == "approval_policy" or key.startswith("approval_policy.")
                    )
            elif argument in {
                "-m",
                "--model",
                "-p",
                "--profile",
                "-i",
                "--image",
                "-C",
                "--cd",
                "--add-dir",
                "--local-provider",
                "--enable",
                "--disable",
                "--remote",
                "--remote-auth-token-env",
            }:
                # An option's value is not another permission option.
                next(arguments, None)

        defaults: list[str] = []
        if not sandbox_overridden:
            defaults.extend(("--sandbox", "danger-full-access"))
        if not approval_overridden:
            defaults.extend(("--ask-for-approval", "never"))
        return defaults

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

    def read_conversation(self, session: Session) -> SessionConversation:
        if session.agent is not self.agent:
            raise ProviderError("CodexProvider 收到了其他 Agent 的会话")

        questions: list[str] = []
        final_answers: list[list[str]] = []
        fallback_answers: list[str | None] = []
        warnings: list[str] = []

        for event in iter_jsonl_snapshot(
            session.transcript_path, warnings, "Codex"
        ):
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            item_type = payload.get("type")

            if item_type == "message":
                role = payload.get("role")
                texts = self._message_texts(payload.get("content"))
                if role == "user":
                    texts = [
                        text
                        for text in texts
                        if not self._is_control_message(text)
                    ]
                    if texts:
                        questions.append("\n\n".join(texts))
                        final_answers.append([])
                        fallback_answers.append(None)
                    continue
                if role != "assistant" or not texts or not questions:
                    continue

                answer = "\n\n".join(texts)
                phase = payload.get("phase")
                if phase == "final_answer":
                    final_answers[-1].append(answer)
                elif phase != "commentary":
                    fallback_answers[-1] = answer
                continue

            if (
                item_type in {"custom_tool_call", "function_call"}
                and questions
                and not final_answers[-1]
            ):
                fallback_answers[-1] = None

        turns = tuple(
            ConversationTurn(
                user=question,
                assistant=(
                    "\n\n".join(answer_parts)
                    if answer_parts
                    else fallback_answer
                ),
            )
            for question, answer_parts, fallback_answer in zip(
                questions, final_answers, fallback_answers
            )
        )
        return SessionConversation(
            session=session,
            turns=turns,
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
