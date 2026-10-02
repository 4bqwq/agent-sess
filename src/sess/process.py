from __future__ import annotations

import errno
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO

from sess.errors import ProviderError


class NativeProcessRunner:
    """Run a native agent with inherited environment and terminal streams."""

    def __init__(self, observation_interval: float = 0.2) -> None:
        self.observation_interval = observation_interval

    def run(self, argv: Sequence[str]) -> int:
        command = self._resolve_command(argv)
        try:
            completed = subprocess.run(command, check=False)
        except OSError as exc:
            raise ProviderError(f"无法启动 {argv[0]}: {exc}") from exc
        return completed.returncode

    def run_observed(
        self, argv: Sequence[str], observer: Callable[[], bool]
    ) -> int:
        """Run an interactive process while observing its persisted session."""

        command = self._resolve_command(argv)
        try:
            process = subprocess.Popen(command)
        except OSError as exc:
            raise ProviderError(f"无法启动 {argv[0]}: {exc}") from exc

        observation_complete = False
        observation_error: Exception | None = None
        interrupted = False
        while True:
            if not observation_complete and observation_error is None:
                try:
                    observation_complete = observer()
                except Exception as exc:
                    observation_error = exc

            try:
                return_code = process.wait(
                    timeout=(
                        None
                        if observation_complete or observation_error is not None
                        else self.observation_interval
                    )
                )
                break
            except subprocess.TimeoutExpired:
                continue
            except KeyboardInterrupt:
                # Interactive children receive the same terminal interrupt.
                # Keep the wrapper alive long enough to observe the JSONL that
                # the child may have just flushed while shutting down.
                interrupted = True

        if observation_error is not None:
            raise observation_error
        return 130 if interrupted else return_code

    @staticmethod
    def _resolve_command(argv: Sequence[str]) -> list[str]:
        if not argv:
            raise ProviderError("原生命令不能为空")
        executable = argv[0]
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise ProviderError(f"当前环境找不到可执行文件: {executable}")
        return [resolved_executable, *argv[1:]]


class ProjectLaunchLock:
    """Allow only one unbound native session launch in a project."""

    def __init__(self, project_dir: Path) -> None:
        self.path = project_dir / ".agent" / ".sess-launch.lock"
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
        except OSError as exc:
            raise ProviderError(f"无法打开 sess 启动锁 {self.path}: {exc}") from exc

        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise ProviderError(
                    "当前项目已有一个由 sess 启动、尚未生成 transcript "
                    "的新会话；请先在该会话提交首次 prompt 或退出"
                ) from exc
            raise ProviderError(f"无法取得 sess 启动锁 {self.path}: {exc}") from exc

        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None
