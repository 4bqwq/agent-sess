from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence

from sess.errors import ProviderError


class NativeProcessRunner:
    """Run a native agent with inherited environment and terminal streams."""

    def run(self, argv: Sequence[str]) -> int:
        if not argv:
            raise ProviderError("原生命令不能为空")
        executable = argv[0]
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise ProviderError(f"当前环境找不到可执行文件: {executable}")
        try:
            completed = subprocess.run(
                [resolved_executable, *argv[1:]], check=False
            )
        except OSError as exc:
            raise ProviderError(f"无法启动 {executable}: {exc}") from exc
        return completed.returncode
