from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sess.paths as paths_module
import sess.providers.claude as claude_module
import sess.providers.codex as codex_module
from sess.errors import MetadataError, ProviderError, TargetError
from sess.metadata import MetadataStore
from sess.models import (
    Agent,
    ConversationTurn,
    DeletionPreview,
    DeletionResult,
    DiscoveryResult,
    Session,
    SessionConversation,
    SessionLaunch,
    SessionTrace,
)
from sess.providers.base import SessionProvider
from sess.providers.codex import CodexProvider
from sess.process import NativeProcessRunner
from sess.service import SessionService


CODEX_ID = "019fec4f-4ba2-7700-9fbf-a55f74a2dca7"
CLAUDE_ID = "491f0baf-9fb7-4d44-908a-c50b1a223a33"
CLAUDE_FORK_ID = "891f0baf-9fb7-4d44-908a-c50b1a223a33"


class StubProvider(SessionProvider):
    def __init__(self, agent: Agent, sessions: tuple[Session, ...]) -> None:
        self.agent = agent
        self.sessions = sessions
        self.conversation_turns: tuple[ConversationTurn, ...] = ()
        self.expected_fork_id: str | None = None

    def discover(self, current_dir: Path) -> DiscoveryResult:
        return DiscoveryResult(self.sessions)

    def snapshot_transcript_paths(self):
        return frozenset(session.transcript_path for session in self.sessions)

    def discover_new(self, current_dir, known_paths):
        return DiscoveryResult(
            tuple(
                session
                for session in self.sessions
                if session.transcript_path not in known_paths
            )
        )

    def new_argv(self, native_args):
        return [self.agent.value, *native_args]

    def resume_argv(self, session_id, native_args):
        return [self.agent.value, "resume", session_id, *native_args]

    def fork_argv(self, session_id, native_args):
        return [self.agent.value, "fork", session_id, *native_args]

    def prepare_fork(self, session_id, native_args):
        return SessionLaunch(
            tuple(self.fork_argv(session_id, native_args)),
            self.expected_fork_id,
        )

    def read_trace(self, session, all_turns):
        return SessionTrace(session=session, events=(), all_turns=all_turns)

    def read_conversation(self, session):
        return SessionConversation(session, self.conversation_turns)

    def preview_delete(self, session):
        return DeletionPreview((str(session.transcript_path),))

    def delete(self, session, runner):
        return DeletionResult(deleted=(str(session.transcript_path),))


class CapturingRunner:
    def __init__(self, return_code: int = 0) -> None:
        self.return_code = return_code
        self.calls: list[list[str]] = []
        self.on_run = None
        self.after_observe = None

    def run(self, argv):
        self.calls.append(list(argv))
        if self.on_run is not None:
            self.on_run(argv)
        return self.return_code

    def run_observed(self, argv, observer):
        self.calls.append(list(argv))
        if self.on_run is not None:
            self.on_run(argv)
        observed = observer()
        if self.after_observe is not None:
            self.after_observe(observed)
        return self.return_code


def make_session(
    agent: Agent,
    session_id: str,
    cwd: Path,
    hours: int,
    *,
    forked_from_id: str | None = None,
) -> Session:
    created = datetime(2026, 8, 11, tzinfo=timezone.utc)
    return Session(
        agent=agent,
        session_id=session_id,
        cwd=cwd,
        created_at=created,
        updated_at=created + timedelta(hours=hours),
        transcript_path=cwd / f"{session_id}.jsonl",
        forked_from_id=forked_from_id,
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


def test_catalog_shares_directory_results_across_providers_and_service(
    tmp_path, monkeypatch
):
    alias = tmp_path / "project-alias"
    other = tmp_path / "other"
    codex = make_session(Agent.CODEX, CODEX_ID, alias, 1)
    claude = make_session(Agent.CLAUDE, CLAUDE_ID, alias, 2)
    excluded_codex = make_session(Agent.CODEX, "excluded-codex", other, 3)
    excluded_claude = make_session(Agent.CLAUDE, "excluded-claude", other, 4)
    providers = (
        StubProvider(Agent.CODEX, (codex, excluded_codex)),
        StubProvider(Agent.CLAUDE, (claude, excluded_claude)),
    )
    service = SessionService(
        tmp_path, providers, MetadataStore(tmp_path), CapturingRunner()
    )
    alias_path = paths_module.canonical_path(alias)
    other_path = paths_module.canonical_path(other)
    current_path = paths_module.canonical_path(service.project_dir)
    calls: list[tuple[str, str]] = []

    def fake_samefile(left, right):
        calls.append((left, right))
        return left == alias_path

    def discover_codex(current_dir):
        assert codex_module.same_directory(alias, current_dir)
        assert not codex_module.same_directory(other, current_dir)
        return DiscoveryResult(providers[0].sessions)

    def discover_claude(current_dir):
        assert claude_module.same_directory(alias, current_dir)
        assert not claude_module.same_directory(other, current_dir)
        return DiscoveryResult(providers[1].sessions)

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)
    monkeypatch.setattr(providers[0], "discover", discover_codex)
    monkeypatch.setattr(providers[1], "discover", discover_claude)

    catalog = service.catalog()

    assert [session.session_id for session in catalog.sessions] == [CLAUDE_ID, CODEX_ID]
    assert calls == [(alias_path, current_path), (other_path, current_path)]


def test_catalog_does_not_reuse_directory_results_from_previous_scan(
    tmp_path, monkeypatch
):
    alias = tmp_path / "project-alias"
    session = make_session(Agent.CODEX, CODEX_ID, alias, 1)
    provider = StubProvider(Agent.CODEX, (session,))
    service = SessionService(
        tmp_path, (provider,), MetadataStore(tmp_path), CapturingRunner()
    )
    matches = True
    calls: list[tuple[str, str]] = []

    def fake_samefile(left, right):
        calls.append((left, right))
        return matches

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)

    assert service.catalog().sessions == (session,)
    matches = False
    assert service.catalog().sessions == ()
    assert len(calls) == 2


@pytest.mark.parametrize("error_type", [OSError, ProviderError])
def test_catalog_restores_directory_scope_after_provider_warning(
    tmp_path, monkeypatch, error_type
):
    alias = tmp_path / "project-alias"
    session = make_session(Agent.CLAUDE, CLAUDE_ID, alias, 1)
    codex = StubProvider(Agent.CODEX, ())
    claude = StubProvider(Agent.CLAUDE, (session,))
    service = SessionService(
        tmp_path, (codex, claude), MetadataStore(tmp_path), CapturingRunner()
    )
    matches = True
    calls: list[tuple[str, str]] = []

    def fake_samefile(left, right):
        calls.append((left, right))
        return matches

    def fail_discover(current_dir):
        assert codex_module.same_directory(alias, current_dir)
        raise error_type("provider unavailable")

    def discover_claude(current_dir):
        assert claude_module.same_directory(alias, current_dir)
        return DiscoveryResult((session,), ("transcript warning",))

    monkeypatch.setattr(paths_module.os.path, "samefile", fake_samefile)
    monkeypatch.setattr(codex, "discover", fail_discover)
    monkeypatch.setattr(claude, "discover", discover_claude)

    catalog = service.catalog()

    assert catalog.sessions == (session,)
    assert catalog.warnings == ("codex: provider unavailable", "transcript warning")
    assert len(calls) == 1
    matches = False
    assert not paths_module.same_directory(alias, service.project_dir)
    assert len(calls) == 2


def test_catalog_restores_directory_scope_after_unhandled_provider_error(
    tmp_path, monkeypatch
):
    alias = tmp_path / "project-alias"
    provider = StubProvider(Agent.CODEX, ())
    service = SessionService(
        tmp_path, (provider,), MetadataStore(tmp_path), CapturingRunner()
    )
    matches = True

    def fail_discover(current_dir):
        assert codex_module.same_directory(alias, current_dir)
        raise RuntimeError("unexpected provider error")

    monkeypatch.setattr(paths_module.os.path, "samefile", lambda _left, _right: matches)
    monkeypatch.setattr(provider, "discover", fail_discover)

    with pytest.raises(RuntimeError, match="unexpected provider error"):
        service.catalog()

    matches = False
    assert not paths_module.same_directory(alias, service.project_dir)


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
    claude_provider = service.providers[Agent.CLAUDE]
    forked = make_session(
        Agent.CLAUDE,
        CLAUDE_FORK_ID,
        tmp_path,
        3,
        forked_from_id=CLAUDE_ID,
    )

    def create_fork(argv):
        if argv[1] == "fork":
            claude_provider.sessions = (*claude_provider.sessions, forked)

    runner.on_run = create_fork

    assert service.resume(CODEX_ID[:8], ["-c", "model=x"]) == 0
    runner.return_code = 130
    result = service.fork(CLAUDE_ID, ["--model", "x"])

    assert result.return_code == 130
    assert result.session is not None
    assert result.session.session_id == CLAUDE_FORK_ID
    assert result.name == "491f0baf-fork1"
    assert runner.calls == [
        ["codex", "resume", CODEX_ID, "-c", "model=x"],
        ["claude", "fork", CLAUDE_ID, "--model", "x"],
    ]
    assert service.metadata.read_names() == {
        f"claude:{CLAUDE_FORK_ID}": "491f0baf-fork1"
    }


def test_new_launches_codex_and_allows_an_omitted_name(tmp_path):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CODEX]
    new_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    created = make_session(Agent.CODEX, new_id, tmp_path, 3)

    def create_session(_argv):
        provider.sessions = (*provider.sessions, created)

    runner.on_run = create_session

    result = service.new(native_args=["--model", "test-model"])

    assert runner.calls == [["codex", "--model", "test-model"]]
    assert result.session is not None
    assert result.session.session_id == new_id
    assert result.name is None
    assert service.metadata.read_names() == {}


def test_named_new_writes_name_before_native_process_returns(tmp_path):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CODEX]
    new_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    created = make_session(Agent.CODEX, new_id, tmp_path, 3)

    def create_session(_argv):
        provider.sessions = (*provider.sessions, created)

    def verify_observation(observed):
        assert observed is True
        assert service.metadata.read_names() == {
            f"codex:{new_id}": "experiment"
        }

    runner.on_run = create_session
    runner.after_observe = verify_observation
    runner.return_code = 130

    result = service.new("experiment")

    assert result.return_code == 130
    assert result.session is not None
    assert result.session.name == "experiment"


@pytest.mark.parametrize("source", ["cli", "vscode"])
def test_real_runner_names_new_session_before_child_exits(tmp_path, monkeypatch, source):
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex-home"
    new_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    transcript = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "11"
        / f"rollout-{new_id}.jsonl"
    )
    metadata = MetadataStore(project)
    provider = CodexProvider(codex_home=codex_home, executable=sys.executable)
    # This child tests live transcript observation, not Codex CLI argument parsing.
    monkeypatch.setattr(
        provider, "new_argv", lambda native_args: [sys.executable, *native_args]
    )
    service = SessionService(
        project,
        (provider,),
        metadata,
        NativeProcessRunner(observation_interval=0.01),
    )
    script = """
import json
import pathlib
import sys
import time

transcript = pathlib.Path(sys.argv[1])
transcript.parent.mkdir(parents=True)
transcript.write_text(
    json.dumps(
        {
            "timestamp": "2026-08-11T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": sys.argv[3],
                "cwd": sys.argv[2],
                "originator": "codex-tui",
                "source": sys.argv[5],
            },
        }
    )
    + "\\n",
    encoding="utf-8",
)
metadata = pathlib.Path(sys.argv[4])
deadline = time.monotonic() + 3
while not metadata.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
raise SystemExit(0 if metadata.exists() else 9)
"""

    result = service.new(
        "experiment",
        native_args=[
            "-c",
            script,
            str(transcript),
            str(project),
            new_id,
            str(metadata.path),
            source,
        ],
    )

    assert result.return_code == 0
    assert result.session is not None
    assert result.session.session_id == new_id
    assert metadata.read_names() == {f"codex:{new_id}": "experiment"}


def test_named_new_without_a_persisted_session_leaves_no_name(tmp_path):
    service, runner = make_service(tmp_path)

    result = service.new("experiment")

    assert result.session is None
    assert result.name is None
    assert service.metadata.read_names() == {}
    assert runner.calls == [["codex"]]


def test_named_new_reports_discovery_warnings_only_after_native_process_returns(
    tmp_path, monkeypatch
):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CODEX]
    warning = "本次 transcript 包含无效 session ID: invalid-id.jsonl"
    monkeypatch.setattr(
        provider,
        "discover_new",
        lambda _project, _known_paths: DiscoveryResult(warnings=(warning,)),
    )
    observations = []
    runner.after_observe = observations.append

    with pytest.raises(ProviderError, match="本次会话") as error:
        service.new("experiment")

    assert observations == [False]
    assert warning in str(error.value)
    assert service.metadata.read_names() == {}


def test_named_new_can_bind_a_session_when_discovery_also_has_warnings(
    tmp_path, monkeypatch
):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CODEX]
    new_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    created = make_session(Agent.CODEX, new_id, tmp_path, 3)
    monkeypatch.setattr(
        provider,
        "discover_new",
        lambda _project, _known_paths: DiscoveryResult(
            (created,), ("trailing incomplete JSON line",)
        ),
    )
    runner.return_code = 130

    result = service.new("experiment")

    assert result.return_code == 130
    assert result.session is not None
    assert result.session.session_id == new_id
    assert service.metadata.read_names() == {f"codex:{new_id}": "experiment"}


def test_named_new_refuses_to_guess_between_multiple_new_sessions(tmp_path):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CODEX]
    created = (
        make_session(
            Agent.CODEX,
            "791f0baf-9fb7-4d44-908a-c50b1a223a33",
            tmp_path,
            3,
        ),
        make_session(
            Agent.CODEX,
            "891f0baf-9fb7-4d44-908a-c50b1a223a33",
            tmp_path,
            4,
        ),
    )

    def create_sessions(_argv):
        provider.sessions = (*provider.sessions, *created)

    runner.on_run = create_sessions

    with pytest.raises(ProviderError, match="唯一识别"):
        service.new("experiment")

    assert service.metadata.read_names() == {}


def test_new_rejects_an_existing_name_before_launch(tmp_path):
    service, runner = make_service(tmp_path)
    service.rename(CODEX_ID, "experiment")

    with pytest.raises(MetadataError, match="已由"):
        service.new("experiment")

    assert not runner.calls


def test_fork_without_a_persisted_session_leaves_no_name(tmp_path):
    service, _ = make_service(tmp_path)

    result = service.fork(CODEX_ID)

    assert result.session is None
    assert result.name is None


def test_fork_refuses_to_guess_when_only_an_unrelated_child_appears(tmp_path):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CODEX]
    unrelated = make_session(
        Agent.CODEX,
        "791f0baf-9fb7-4d44-908a-c50b1a223a33",
        tmp_path,
        3,
        forked_from_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    def create_unrelated(_argv):
        provider.sessions = (*provider.sessions, unrelated)

    runner.on_run = create_unrelated

    with pytest.raises(ProviderError, match="会话保持未命名"):
        service.fork(CODEX_ID)


def test_fork_uses_a_provider_reserved_child_id(tmp_path):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CLAUDE]
    provider.expected_fork_id = CLAUDE_FORK_ID
    forked = make_session(Agent.CLAUDE, CLAUDE_FORK_ID, tmp_path, 3)

    def create_fork(_argv):
        provider.sessions = (*provider.sessions, forked)

    runner.on_run = create_fork

    result = service.fork(CLAUDE_ID)

    assert result.session is not None
    assert result.session.session_id == CLAUDE_FORK_ID
    assert result.name == "491f0baf-fork1"


def test_fork_name_extends_parent_prefix_until_it_is_unique(tmp_path):
    similar_parent_id = "491f0baf-afb7-4d44-908a-c50b1a223a33"
    parent = make_session(Agent.CLAUDE, CLAUDE_ID, tmp_path, 1)
    similar_parent = make_session(
        Agent.CLAUDE, similar_parent_id, tmp_path, 2
    )
    provider = StubProvider(Agent.CLAUDE, (parent, similar_parent))
    forked = make_session(
        Agent.CLAUDE,
        CLAUDE_FORK_ID,
        tmp_path,
        3,
        forked_from_id=CLAUDE_ID,
    )
    runner = CapturingRunner()

    def create_fork(_argv):
        provider.sessions = (*provider.sessions, forked)

    runner.on_run = create_fork
    service = SessionService(
        tmp_path,
        (provider,),
        MetadataStore(tmp_path),
        runner,
    )

    result = service.fork(CLAUDE_ID)

    assert result.name == "491f0baf-9-fork1"


def test_explicit_fork_name_does_not_consume_automatic_number(tmp_path):
    service, runner = make_service(tmp_path)
    provider = service.providers[Agent.CLAUDE]
    manual_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    automatic_id = "891f0baf-9fb7-4d44-908a-c50b1a223a33"
    launches = [
        make_session(
            Agent.CLAUDE,
            manual_id,
            tmp_path,
            3,
            forked_from_id=CLAUDE_ID,
        ),
        make_session(
            Agent.CLAUDE,
            automatic_id,
            tmp_path,
            4,
            forked_from_id=CLAUDE_ID,
        ),
    ]

    def create_next(_argv):
        provider.sessions = (*provider.sessions, launches.pop(0))

    runner.on_run = create_next

    manual = service.new("experiment", fork_target=CLAUDE_ID)
    automatic = service.fork(CLAUDE_ID)

    assert manual.name == "experiment"
    assert automatic.name == "491f0baf-fork1"
    assert service.metadata.read_names() == {
        f"claude:{manual_id}": "experiment",
        f"claude:{automatic_id}": "491f0baf-fork1",
    }


def test_read_trace_resolves_prefix_and_delegates_to_provider(tmp_path):
    service, _ = make_service(tmp_path)

    trace = service.read_trace(CODEX_ID[:8], all_turns=True)

    assert trace.session.session_id == CODEX_ID
    assert trace.all_turns is True


def test_transcript_path_is_absolute_and_conversation_is_limited(tmp_path):
    service, _ = make_service(tmp_path)
    provider = service.providers[Agent.CODEX]
    provider.conversation_turns = (
        ConversationTurn("old question", "old answer"),
        ConversationTurn("new question", "new answer"),
    )

    assert service.transcript_path(CODEX_ID) == (
        tmp_path / f"{CODEX_ID}.jsonl"
    ).resolve()
    conversation = service.read_conversation(CODEX_ID, 1)
    assert conversation.turns == (
        ConversationTurn("new question", "new answer"),
    )


def test_delete_removes_name_only_after_provider_success(tmp_path):
    service, _ = make_service(tmp_path)
    session = service.rename(CODEX_ID, "feature-a")

    result = service.delete(session)

    assert result.success
    assert service.metadata.read_names() == {}
