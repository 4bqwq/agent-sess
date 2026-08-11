from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path


class Agent(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"


class TraceEventKind(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Session:
    agent: Agent
    session_id: str
    cwd: Path
    created_at: datetime
    updated_at: datetime
    transcript_path: Path
    name: str | None = None

    @property
    def metadata_key(self) -> str:
        return f"{self.agent.value}:{self.session_id}"

    def with_name(self, name: str | None) -> "Session":
        return replace(self, name=name)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    sessions: tuple[Session, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Catalog:
    sessions: tuple[Session, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeletionResult:
    deleted: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()

    @property
    def success(self) -> bool:
        return not self.remaining


@dataclass(frozen=True, slots=True)
class TraceEvent:
    kind: TraceEventKind
    content: str
    timestamp: datetime | None = None
    label: str | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class SessionTrace:
    session: Session
    events: tuple[TraceEvent, ...]
    all_turns: bool
    warnings: tuple[str, ...] = ()
