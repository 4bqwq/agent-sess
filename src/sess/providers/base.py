from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from sess.errors import ProviderError
from sess.models import (
    Agent,
    DeletionPreview,
    DeletionResult,
    DiscoveryResult,
    Session,
    SessionConversation,
    SessionLaunch,
    SessionTrace,
)
from sess.process import NativeProcessRunner


class SessionProvider(ABC):
    agent: Agent

    @abstractmethod
    def discover(self, current_dir: Path) -> DiscoveryResult:
        raise NotImplementedError

    @abstractmethod
    def snapshot_transcript_paths(self) -> frozenset[Path]:
        raise NotImplementedError

    @abstractmethod
    def discover_new(
        self, current_dir: Path, known_paths: frozenset[Path]
    ) -> DiscoveryResult:
        raise NotImplementedError

    def new_argv(self, native_args: Sequence[str]) -> list[str]:
        raise ProviderError(f"{self.agent.value} 不支持直接启动新会话")

    @abstractmethod
    def resume_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def fork_argv(self, session_id: str, native_args: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def prepare_fork(
        self, session_id: str, native_args: Sequence[str]
    ) -> SessionLaunch:
        return SessionLaunch(tuple(self.fork_argv(session_id, native_args)))

    @abstractmethod
    def read_trace(self, session: Session, all_turns: bool) -> SessionTrace:
        raise NotImplementedError

    @abstractmethod
    def read_conversation(self, session: Session) -> SessionConversation:
        raise NotImplementedError

    @abstractmethod
    def preview_delete(self, session: Session) -> DeletionPreview:
        raise NotImplementedError

    @abstractmethod
    def delete(
        self, session: Session, runner: NativeProcessRunner
    ) -> DeletionResult:
        raise NotImplementedError
