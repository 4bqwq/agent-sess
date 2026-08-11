from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from sess.errors import ProviderError
from sess.models import (
    Agent,
    DeletionPreview,
    DeletionResult,
    DiscoveryResult,
    Session,
)
from sess.paths import canonical_path, is_within
from sess.process import NativeProcessRunner
from sess.providers.base import SessionProvider
from sess.providers.common import is_full_uuid, parse_timestamp


class ClaudeProvider(SessionProvider):
    agent = Agent.CLAUDE
    _PROJECT_SESSION_KEYS = {
        "lastAPIDuration",
        "lastAPIDurationWithoutRetries",
        "lastCost",
        "lastDuration",
        "lastFpsAverage",
        "lastFpsLow1Pct",
        "lastGracefulShutdown",
        "lastHintSessionId",
        "lastLinesAdded",
        "lastLinesRemoved",
        "lastModelUsage",
        "lastSessionFirstPrompt",
        "lastSessionId",
        "lastSessionMetrics",
        "lastSessionModified",
        "lastStartTime",
        "lastToolDuration",
        "lastTotalCacheCreationInputTokens",
        "lastTotalCacheReadInputTokens",
        "lastTotalInputTokens",
        "lastTotalOutputTokens",
        "lastTotalWebSearchRequests",
    }

    def __init__(
        self,
        config_dir: Path | None = None,
        state_file: Path | None = None,
        executable: str = "claude",
    ) -> None:
        configured_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir is not None:
            self.config_dir = config_dir
            config_was_overridden = True
        elif configured_dir:
            self.config_dir = Path(configured_dir).expanduser()
            config_was_overridden = True
        else:
            self.config_dir = Path.home() / ".claude"
            config_was_overridden = False

        if state_file is not None:
            self.state_file = state_file
        elif config_was_overridden:
            self.state_file = self.config_dir / ".claude.json"
        else:
            self.state_file = Path.home() / ".claude.json"
        self.executable = executable

    def discover(self, current_dir: Path) -> DiscoveryResult:
        projects_root = self.config_dir / "projects"
        if not projects_root.exists():
            return DiscoveryResult()

        target_cwd = canonical_path(current_dir)
        sessions: list[Session] = []
        warnings: list[str] = []
        try:
            transcript_paths = sorted(projects_root.glob("*/*.jsonl"))
        except OSError as exc:
            return DiscoveryResult(
                warnings=(f"无法扫描 Claude Code 会话目录 {projects_root}: {exc}",)
            )

        for transcript_path in transcript_paths:
            session, file_warnings = self._read_transcript(transcript_path, target_cwd)
            warnings.extend(file_warnings)
            if session is not None:
                sessions.append(session)
        return DiscoveryResult(tuple(sessions), tuple(warnings))

    def _read_transcript(
        self, transcript_path: Path, target_cwd: str
    ) -> tuple[Session | None, list[str]]:
        warnings: list[str] = []
        session_id: str | None = None
        cwd: str | None = None
        created_at = None
        updated_at = None
        entrypoints: set[str] = set()
        has_non_interactive_marker = False
        is_sidechain = False

        try:
            with transcript_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        warnings.append(
                            f"跳过 Claude transcript 的无效 JSON 行 "
                            f"{transcript_path}:{line_number}: {exc.msg}"
                        )
                        continue
                    if not isinstance(event, dict):
                        continue

                    timestamp = parse_timestamp(event.get("timestamp"))
                    if timestamp is not None:
                        if created_at is None or timestamp < created_at:
                            created_at = timestamp
                        if updated_at is None or timestamp > updated_at:
                            updated_at = timestamp

                    raw_id = event.get("sessionId")
                    if isinstance(raw_id, str) and raw_id:
                        session_id = session_id or raw_id
                    raw_cwd = event.get("cwd")
                    if isinstance(raw_cwd, str) and raw_cwd:
                        cwd = cwd or raw_cwd

                    entrypoint = event.get("entrypoint")
                    prompt_source = event.get("promptSource")
                    if isinstance(entrypoint, str):
                        entrypoints.add(entrypoint)
                    if (
                        isinstance(entrypoint, str)
                        and entrypoint.startswith("sdk")
                    ) or prompt_source == "sdk":
                        has_non_interactive_marker = True
                    if event.get("isSidechain") is True:
                        is_sidechain = True
        except (OSError, UnicodeError) as exc:
            warnings.append(f"无法读取 Claude transcript {transcript_path}: {exc}")
            return None, warnings

        if has_non_interactive_marker or is_sidechain:
            return None, warnings
        if entrypoints and "cli" not in entrypoints:
            return None, warnings
        if session_id is None or cwd is None or created_at is None or updated_at is None:
            return None, warnings
        if not is_full_uuid(session_id):
            warnings.append(f"Claude transcript 包含无效 session ID: {transcript_path}")
            return None, warnings
        if canonical_path(cwd) != target_cwd:
            return None, warnings

        if transcript_path.stem != session_id:
            warnings.append(
                f"Claude transcript 文件名与 sessionId 不一致，已跳过: {transcript_path}"
            )
            return None, warnings
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
        return [self.executable, "--resume", session_id, *native_args]

    def fork_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        return [
            self.executable,
            "--resume",
            session_id,
            "--fork-session",
            *native_args,
        ]

    def preview_delete(self, session: Session) -> DeletionPreview:
        self._assert_deletable_session(session)
        self._assert_not_active(session.session_id)
        dedicated_paths = self._dedicated_paths(session)
        items = [str(path) for path in dedicated_paths if path.exists()]

        history_count = self._count_jsonl_matches(
            self.config_dir / "history.jsonl",
            lambda value: value.get("sessionId") == session.session_id,
        )
        if history_count:
            items.append(
                f"{self.config_dir / 'history.jsonl'} 中与该 session 匹配的 {history_count} 行"
            )
        if self._state_points_to_session(session):
            items.append(f"{self.state_file} 中该项目的 lastSession 状态")
        if not items:
            items.append(f"Claude Code session {session.session_id}（没有找到可删除对象）")
        return DeletionPreview(tuple(items))

    def delete(
        self, session: Session, runner: NativeProcessRunner
    ) -> DeletionResult:
        del runner  # Claude Code has no native command for deleting one local session.
        self._assert_deletable_session(session)
        self._assert_not_active(session.session_id)
        dedicated_paths = self._dedicated_paths(session)

        deleted: list[str] = []
        remaining: list[str] = []

        history_path = self.config_dir / "history.jsonl"
        try:
            removed_lines = self._filter_jsonl(
                history_path,
                lambda value: value.get("sessionId") == session.session_id,
            )
            if removed_lines:
                deleted.append(f"{history_path} 中的 {removed_lines} 条 prompt history")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            remaining.append(f"{history_path}: {exc}")

        try:
            if self._clear_project_session_state(session):
                deleted.append(f"{self.state_file} 中该项目的 lastSession 状态")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            remaining.append(f"{self.state_file}: {exc}")

        for path in dedicated_paths:
            if not path.exists() and not path.is_symlink():
                continue
            try:
                self._remove_path(path)
                deleted.append(str(path))
            except OSError as exc:
                remaining.append(f"{path}: {exc}")

        for path in dedicated_paths:
            if path.exists() or path.is_symlink():
                rendered = str(path)
                if not any(item.startswith(rendered) for item in remaining):
                    remaining.append(rendered)

        return DeletionResult(tuple(deleted), tuple(remaining))

    def _assert_deletable_session(self, session: Session) -> None:
        projects_root = self.config_dir / "projects"
        expected_name = f"{session.session_id}.jsonl"
        if session.agent is not self.agent:
            raise ProviderError("ClaudeProvider 收到了其他 Agent 的会话")
        if session.transcript_path.name != expected_name or not is_within(
            session.transcript_path, projects_root
        ):
            raise ProviderError(
                f"拒绝删除不在 Claude projects 目录中的 transcript: {session.transcript_path}"
            )

    def _dedicated_paths(self, session: Session) -> tuple[Path, ...]:
        session_id = session.session_id
        candidates: set[Path] = {
            session.transcript_path,
            session.transcript_path.with_suffix(""),
            self.config_dir / "file-history" / session_id,
            self.config_dir / "session-env" / session_id,
            self.config_dir / "tasks" / session_id,
        }

        for directory_name in ("debug", "telemetry"):
            directory = self.config_dir / directory_name
            if not directory.exists():
                continue
            try:
                candidates.update(directory.rglob(f"*{session_id}*"))
            except OSError as exc:
                raise ProviderError(f"无法扫描 Claude Code sidecar 目录 {directory}: {exc}") from exc

        sessions_dir = self.config_dir / "sessions"
        if sessions_dir.exists():
            try:
                registry_files = sessions_dir.glob("*.json")
                for registry_file in registry_files:
                    record = self._read_json_object(registry_file)
                    if record is not None and record.get("sessionId") == session_id:
                        candidates.add(registry_file)
            except OSError as exc:
                raise ProviderError(
                    f"无法扫描 Claude Code 活跃会话目录 {sessions_dir}: {exc}"
                ) from exc

        validated = [path for path in candidates if is_within(path, self.config_dir)]
        return tuple(sorted(validated, key=lambda value: (len(value.parts), str(value))))

    def _assert_not_active(self, session_id: str) -> None:
        sessions_dir = self.config_dir / "sessions"
        if not sessions_dir.exists():
            return
        try:
            registry_files = sessions_dir.glob("*.json")
            for registry_file in registry_files:
                record = self._read_json_object(registry_file)
                if record is None or record.get("sessionId") != session_id:
                    continue
                pid = record.get("pid")
                if isinstance(pid, int) and self._pid_is_alive(pid):
                    raise ProviderError(
                        f"Claude Code session {session_id} 仍由进程 {pid} 使用，请先退出该会话"
                    )
        except OSError as exc:
            raise ProviderError(f"无法检查 Claude Code 活跃会话: {exc}") from exc

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            return ClaudeProvider._windows_pid_is_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _windows_pid_is_alive(pid: int) -> bool:
        """Query a Windows process without sending it a signal."""

        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, object] | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _state_points_to_session(self, session: Session) -> bool:
        document = self._read_json_object(self.state_file)
        if document is None:
            return False
        projects = document.get("projects")
        if not isinstance(projects, dict):
            return False
        target_cwd = canonical_path(session.cwd)
        for project_path, value in projects.items():
            if (
                isinstance(project_path, str)
                and isinstance(value, dict)
                and canonical_path(project_path) == target_cwd
                and value.get("lastSessionId") == session.session_id
            ):
                return True
        return False

    def _clear_project_session_state(self, session: Session) -> bool:
        if not self.state_file.exists():
            return False
        with self.state_file.open("r", encoding="utf-8") as handle:
            signature = self._stat_signature(os.fstat(handle.fileno()))
            document = json.load(handle)
        if not isinstance(document, dict):
            raise json.JSONDecodeError("顶层不是 JSON object", "", 0)
        projects = document.get("projects")
        if not isinstance(projects, dict):
            return False

        target_cwd = canonical_path(session.cwd)
        changed = False
        for project_path, value in projects.items():
            if not isinstance(project_path, str) or not isinstance(value, dict):
                continue
            if canonical_path(project_path) != target_cwd:
                continue
            if value.get("lastSessionId") != session.session_id:
                continue
            for key in self._PROJECT_SESSION_KEYS:
                if key in value:
                    del value[key]
                    changed = True
        if changed:
            self._atomic_write_json(self.state_file, document, signature)
        return changed

    @staticmethod
    def _count_jsonl_matches(
        path: Path, predicate: Callable[[dict[str, object]], bool]
    ) -> int:
        if not path.exists():
            return 0
        count = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and predicate(value):
                    count += 1
        return count

    def _filter_jsonl(
        self, path: Path, predicate: Callable[[dict[str, object]], bool]
    ) -> int:
        if not path.exists():
            return 0
        kept_lines: list[str] = []
        removed = 0
        with path.open("r", encoding="utf-8") as handle:
            signature = self._stat_signature(os.fstat(handle.fileno()))
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    kept_lines.append(line)
                    continue
                if isinstance(value, dict) and predicate(value):
                    removed += 1
                else:
                    kept_lines.append(line)
        if removed:
            self._atomic_write_text(path, "".join(kept_lines), signature)
        return removed

    @classmethod
    def _atomic_write_json(
        cls,
        path: Path,
        value: object,
        expected_signature: tuple[int, int, int, int] | None = None,
    ) -> None:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        cls._atomic_write_text(path, rendered, expected_signature)

    @staticmethod
    def _atomic_write_text(
        path: Path,
        content: str,
        expected_signature: tuple[int, int, int, int] | None = None,
    ) -> None:
        current_stat = path.stat()
        if (
            expected_signature is not None
            and ClaudeProvider._stat_signature(current_stat) != expected_signature
        ):
            raise OSError(f"文件在准备删除时已被其他进程修改: {path}")
        original_mode = stat.S_IMODE(current_stat.st_mode)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, original_mode)
            if (
                expected_signature is not None
                and ClaudeProvider._stat_signature(path.stat()) != expected_signature
            ):
                raise OSError(f"文件在准备删除时已被其他进程修改: {path}")
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int]:
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
