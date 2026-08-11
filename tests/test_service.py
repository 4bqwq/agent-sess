from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sess.errors import TargetError
from sess.metadata import MetadataStore
from sess.models import (
    Agent,
    DeletionPreview,
    DeletionResult,
    DiscoveryResult,
    Session,
)
from sess.providers.base import SessionProvider
from sess.service import SessionService


CODEX_ID = "019fec4f-4ba2-7700-9fbf-a55f74a2dca7"
CLAUDE_ID = "491f0baf-9fb7-4d44-908a-c50b1a223a33"


class StubProvider(SessionProvider):
    def __init__(self, agent: Agent, sessions: tuple[Session, ...]) -> None:
        self.agent = agent
        self.sessions = sessions

    def discover(self, current_dir: Path) -> DiscoveryResult:
        return DiscoveryResult(self.sessions)

    def resume_argv(self, session_id, native_args):
        return [self.agent.value, "resume", session_id, *native_args]

    def fork_argv(self, session_id, native_args):
        return [self.agent.value, "fork", session_id, *native_args]

    def preview_delete(self, session):
        return DeletionPreview((str(session.transcript_path),))

    def delete(self, session, runner):
        return DeletionResult(deleted=(str(session.transcript_path),))


class CapturingRunner:
    def __init__(self, return_code: int = 0) -> None:
        self.return_code = return_code
        self.calls: list[list[str]] = []

    def run(self, argv):
        self.calls.append(list(argv))
        return self.return_code


def make_session(agent: Agent, session_id: str, cwd: Path, hours: int) -> Session:
    created = datetime(2026, 8, 11, tzinfo=timezone.utc)
    return Session(
        agent=agent,
        session_id=session_id,
        cwd=cwd,
        created_at=created,
        updated_at=created + timedelta(hours=hours),
        transcript_path=cwd / f"{session_id}.jsonl",
    )


def make_service(tmp_path):
    codex = make_session(Agent.CODEX, CODEX_ID, tmp_path, 1)
    claude = make_session(Agent.CLAUDE, CLAUDE_ID, tmp_path, 2)
    providers = (
        StubProvider(Agent.CODEX, (codex,)),
        StubProvider(Agent.CLAUDE, (claude,)),
    )
    runner = CapturingRunner()
    metadata = MetadataStore(tmp_path)
    service = SessionService(tmp_path, providers, metadata, runner)
    return service, runner


def test_catalog_sorts_by_updated_and_attaches_names(tmp_path):
    service, _ = make_service(tmp_path)
    service.metadata.set_name(f"codex:{CODEX_ID}", "feature-a")

    catalog = service.catalog()

    assert [session.session_id for session in catalog.sessions] == [CLAUDE_ID, CODEX_ID]
    assert catalog.sessions[1].name == "feature-a"


def test_catalog_defensively_excludes_sessions_from_child_directories(tmp_path):
    current = make_session(Agent.CODEX, CODEX_ID, tmp_path, 1)
    child = make_session(Agent.CLAUDE, CLAUDE_ID, tmp_path / "child", 2)
    providers = (
        StubProvider(Agent.CODEX, (current,)),
        StubProvider(Agent.CLAUDE, (child,)),
    )
    service = SessionService(
        tmp_path,
        providers,
        MetadataStore(tmp_path),
        CapturingRunner(),
    )

    catalog = service.catalog()

    assert [session.session_id for session in catalog.sessions] == [CODEX_ID]


def test_resolve_uses_full_id_or_exact_name(tmp_path):
    service, _ = make_service(tmp_path)
    service.rename(CODEX_ID, "feature-a")

    assert service.resolve(CODEX_ID).agent is Agent.CODEX
    assert service.resolve("feature-a").session_id == CODEX_ID
    assert service.resolve(CODEX_ID[:8]).session_id == CODEX_ID
    assert service.resolve(CODEX_ID[:8].upper()).session_id == CODEX_ID
    with pytest.raises(TargetError):
        service.resolve("feature")


def test_resolve_requires_at_least_eight_characters_for_id_prefix(tmp_path):
    service, _ = make_service(tmp_path)

    with pytest.raises(TargetError, match="至少需要 8 个字符"):
        service.resolve(CODEX_ID[:7])


def test_exact_name_takes_precedence_over_an_id_prefix(tmp_path):
    service, _ = make_service(tmp_path)
    service.rename(CLAUDE_ID, CODEX_ID[:8])

    assert service.resolve(CODEX_ID[:8]).session_id == CLAUDE_ID


def test_ambiguous_id_prefix_lists_all_candidates(tmp_path):
    similar_id = "019fec4f-ffff-4000-8000-000000000001"
    first = make_session(Agent.CODEX, CODEX_ID, tmp_path, 1)
    second = make_session(Agent.CODEX, similar_id, tmp_path, 2)
    provider = StubProvider(Agent.CODEX, (first, second))
    service = SessionService(
        tmp_path,
        (provider,),
        MetadataStore(tmp_path),
        CapturingRunner(),
    )

    with pytest.raises(TargetError, match="对应多个会话") as error:
        service.resolve(CODEX_ID[:8])

    assert CODEX_ID in str(error.value)
    assert similar_id in str(error.value)


def test_resume_and_fork_pass_native_arguments_to_provider(tmp_path):
    service, runner = make_service(tmp_path)

    assert service.resume(CODEX_ID[:8], ["-c", "model=x"]) == 0
    assert service.fork(CLAUDE_ID, ["--model", "x"]) == 0
    assert runner.calls == [
        ["codex", "resume", CODEX_ID, "-c", "model=x"],
        ["claude", "fork", CLAUDE_ID, "--model", "x"],
    ]


def test_delete_removes_name_only_after_provider_success(tmp_path):
    service, _ = make_service(tmp_path)
    session = service.rename(CODEX_ID, "feature-a")

    result = service.delete(session)

    assert result.success
    assert service.metadata.read_names() == {}
