from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from sess.models import (
    Agent,
    DeletionPreview,
    DeletionResult,
    DiscoveryResult,
    Session,
    SessionTrace,
)
from sess.process import NativeProcessRunner


class SessionProvider(ABC):
    agent: Agent

    @abstractmethod
    def discover(self, current_dir: Path) -> DiscoveryResult:
        raise NotImplementedError

    @abstractmethod
    def resume_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def fork_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def read_trace(self, session: Session, all_turns: bool) -> SessionTrace:
        raise NotImplementedError

    @abstractmethod
    def preview_delete(self, session: Session) -> DeletionPreview:
        raise NotImplementedError

    @abstractmethod
    def delete(
        self, session: Session, runner: NativeProcessRunner
    ) -> DeletionResult:
        raise NotImplementedError
