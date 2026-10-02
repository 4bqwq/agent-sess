from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sess.providers.claude as claude_module
from sess.errors import ProviderError
from sess.models import Agent, Session, TraceEventKind
from sess.providers.claude import ClaudeProvider


SESSION_ID = "491f0baf-9fb7-4d07-92f0-2ccce34b071a"
OTHER_ID = "6de5a266-8404-4cf0-8785-aae4851a0aa8"


def write_jsonl(path: Path, events: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def claude_event(
    cwd: Path,
    session_id: str,
    timestamp: str,
    *,
    entrypoint: str,
    prompt_source: str,
):
    return {
        "type": "user",
        "sessionId": session_id,
        "cwd": str(cwd),
        "timestamp": timestamp,
        "entrypoint": entrypoint,
        "promptSource": prompt_source,
        "isSidechain": False,
    }


def make_provider(tmp_path: Path) -> ClaudeProvider:
    config_dir = tmp_path / "claude-home"
    return ClaudeProvider(
        config_dir=config_dir,
        state_file=config_dir / ".claude.json",
        executable="claude-test",
    )


def test_discover_excludes_sdk_and_requires_exact_cwd(tmp_path):
    provider = make_provider(tmp_path)
    current = tmp_path / "project"
    child = current / "child"
    child.mkdir(parents=True)
    project_store = provider.config_dir / "projects" / "encoded"

    write_jsonl(
        project_store / f"{SESSION_ID}.jsonl",
        [
            claude_event(
                current,
                SESSION_ID,
                "2026-08-11T01:00:00Z",
                entrypoint="cli",
                prompt_source="typed",
            ),
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "cwd": str(current),
                "timestamp": "2026-08-11T02:00:00Z",
                "entrypoint": "cli",
                "isSidechain": False,
            },
        ],
    )
    write_jsonl(
        project_store / f"{OTHER_ID}.jsonl",
        [
            claude_event(
                current,
                OTHER_ID,
                "2026-08-11T03:00:00Z",
                entrypoint="sdk-cli",
                prompt_source="sdk",
            )
        ],
    )
    child_id = "11111111-1111-4111-8111-111111111111"
    write_jsonl(
        project_store / f"{child_id}.jsonl",
        [
            claude_event(
                child,
                child_id,
                "2026-08-11T04:00:00Z",
                entrypoint="cli",
                prompt_source="typed",
            )
        ],
    )

    result = provider.discover(current)

    assert [session.session_id for session in result.sessions] == [SESSION_ID]
    assert result.sessions[0].updated_at.hour == 2


def test_discover_new_only_reads_paths_created_after_snapshot(tmp_path):
    provider = make_provider(tmp_path)
    current = tmp_path / "project"
    current.mkdir()
    project_store = provider.config_dir / "projects" / "encoded"
    write_jsonl(
        project_store / f"{SESSION_ID}.jsonl",
        [
            claude_event(
                current,
                SESSION_ID,
                "2026-08-11T01:00:00Z",
                entrypoint="cli",
                prompt_source="typed",
            )
        ],
    )
    snapshot = provider.snapshot_transcript_paths()
    write_jsonl(
        project_store / f"{OTHER_ID}.jsonl",
        [
            claude_event(
                current,
                OTHER_ID,
                "2026-08-11T02:00:00Z",
                entrypoint="cli",
                prompt_source="typed",
            )
        ],
    )

    result = provider.discover_new(current, snapshot)

    assert [session.session_id for session in result.sessions] == [OTHER_ID]


def test_claude_commands_preserve_forwarded_argument_order():
    provider = ClaudeProvider(
        config_dir=Path("/unused"),
        state_file=Path("/unused/state.json"),
        executable="claude-test",
    )
    native_args = ["--model", "test-model", "initial prompt"]

    assert provider.resume_argv(SESSION_ID, native_args) == [
        "claude-test",
        "--resume",
        SESSION_ID,
        *native_args,
    ]
    assert provider.fork_argv(SESSION_ID, native_args) == [
        "claude-test",
        "--resume",
        SESSION_ID,
        "--fork-session",
        *native_args,
    ]


def test_claude_prepare_fork_reserves_a_child_session_id(tmp_path, monkeypatch):
    provider = make_provider(tmp_path)
    child_id = uuid.UUID(OTHER_ID)
    monkeypatch.setattr(claude_module.uuid, "uuid4", lambda: child_id)

    launch = provider.prepare_fork(SESSION_ID, ["--model", "test-model"])

    assert launch.expected_session_id == OTHER_ID
    assert list(launch.argv) == [
        "claude-test",
        "--resume",
        SESSION_ID,
        "--fork-session",
        "--session-id",
        OTHER_ID,
        "--model",
        "test-model",
    ]


def test_claude_prepare_fork_uses_a_forwarded_child_session_id(tmp_path):
    provider = make_provider(tmp_path)

    launch = provider.prepare_fork(
        SESSION_ID,
        ["--session-id", OTHER_ID, "--model", "test-model"],
    )

    assert launch.expected_session_id == OTHER_ID
    assert list(launch.argv) == [
        "claude-test",
        "--resume",
        SESSION_ID,
        "--fork-session",
        "--session-id",
        OTHER_ID,
        "--model",
        "test-model",
    ]


@pytest.mark.parametrize(
    "flag",
    ["--resume", "-r", "--continue", "-c", "--session-id=x", "--fork-session"],
)
def test_claude_passes_session_selection_flags_to_native_cli(flag):
    provider = make_provider(Path("/unused"))

    assert provider.resume_argv(SESSION_ID, [flag])[-1] == flag


def test_claude_trace_uses_latest_human_turn_and_skips_thinking(tmp_path):
    provider = make_provider(tmp_path)
    current = tmp_path / "project"
    current.mkdir()
    transcript = (
        provider.config_dir / "projects" / "encoded" / f"{SESSION_ID}.jsonl"
    )
    events = [
        {
            "type": "user",
            "timestamp": "2026-08-11T01:00:00Z",
            "message": {"role": "user", "content": "old request"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-11T01:00:01Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "old answer"}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-08-11T02:00:00Z",
            "origin": {"kind": "human"},
            "message": {"role": "user", "content": "latest request"},
        },
        {
            "type": "user",
            "timestamp": "2026-08-11T02:00:01Z",
            "message": {
                "role": "user",
                "content": "<system-reminder>hidden instruction</system-reminder>",
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-11T02:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "hidden thinking",
                        "signature": "signed",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-11T02:00:03Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "checking files"}],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-11T02:00:04Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "rg session"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-08-11T02:00:05Z",
            "sourceToolAssistantUUID": "assistant-1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "found session",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-11T02:00:06Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "latest answer"}],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-11T02:00:07Z",
            "error": "authentication_failed",
            "isApiErrorMessage": True,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Please run /login · API Error: 401"}
                ],
            },
        },
    ]
    write_jsonl(transcript, events)
    timestamp = datetime(2026, 8, 11, tzinfo=timezone.utc)
    session = Session(
        agent=Agent.CLAUDE,
        session_id=SESSION_ID,
        cwd=current,
        created_at=timestamp,
        updated_at=timestamp,
        transcript_path=transcript,
    )

    latest = provider.read_trace(session, all_turns=False)
    latest_text = "\n".join(event.content for event in latest.events)
    assert [event.kind for event in latest.events] == [
        TraceEventKind.USER,
        TraceEventKind.ASSISTANT,
        TraceEventKind.TOOL_CALL,
        TraceEventKind.TOOL_RESULT,
        TraceEventKind.ASSISTANT,
        TraceEventKind.ERROR,
    ]
    assert "latest request" in latest_text
    assert "old request" not in latest_text
    assert "hidden instruction" not in latest_text
    assert "hidden thinking" not in latest_text
    assert "API Error: 401" in latest_text
    assert latest.events[2].label == "Bash"
    assert latest.events[3].label == "Bash"

    complete = provider.read_trace(session, all_turns=True)
    complete_text = "\n".join(event.content for event in complete.events)
    assert "old request" in complete_text
    assert "old answer" in complete_text

    conversation = provider.read_conversation(session)
    assert [turn.user for turn in conversation.turns] == [
        "old request",
        "latest request",
    ]
    assert [turn.assistant for turn in conversation.turns] == [
        "old answer",
        "latest answer",
    ]
    assert "checking files" not in "\n".join(
        turn.assistant or "" for turn in conversation.turns
    )


def create_deletion_fixture(tmp_path: Path):
    provider = make_provider(tmp_path)
    current = tmp_path / "project"
    current.mkdir()
    transcript = (
        provider.config_dir / "projects" / "encoded" / f"{SESSION_ID}.jsonl"
    )
    write_jsonl(
        transcript,
        [
            claude_event(
                current,
                SESSION_ID,
                "2026-08-11T01:00:00Z",
                entrypoint="cli",
                prompt_source="typed",
            )
        ],
    )
    for directory in (
        transcript.with_suffix(""),
        provider.config_dir / "file-history" / SESSION_ID,
        provider.config_dir / "session-env" / SESSION_ID,
        provider.config_dir / "tasks" / SESSION_ID,
    ):
        directory.mkdir(parents=True)
        (directory / "item").write_text("fixture", encoding="utf-8")

    debug_file = provider.config_dir / "debug" / f"debug-{SESSION_ID}.txt"
    telemetry_file = provider.config_dir / "telemetry" / f"event.{SESSION_ID}.json"
    debug_file.parent.mkdir(parents=True)
    telemetry_file.parent.mkdir(parents=True)
    debug_file.write_text("debug", encoding="utf-8")
    telemetry_file.write_text("{}", encoding="utf-8")

    history = provider.config_dir / "history.jsonl"
    history.write_text(
        json.dumps({"sessionId": SESSION_ID, "display": "remove"})
        + "\n"
        + "not-json\n"
        + json.dumps({"sessionId": OTHER_ID, "display": "keep"})
        + "\n",
        encoding="utf-8",
    )
    provider.state_file.write_text(
        json.dumps(
            {
                "projects": {
                    str(current): {
                        "hasTrustDialogAccepted": True,
                        "lastSessionId": SESSION_ID,
                        "lastSessionMetrics": {"turns": 2},
                        "lastVersionBase": "2.1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry = provider.config_dir / "sessions" / "99999999.json"
    registry.parent.mkdir()
    registry.write_text(
        json.dumps({"sessionId": SESSION_ID, "pid": 99_999_999}), encoding="utf-8"
    )

    timestamp = datetime(2026, 8, 11, tzinfo=timezone.utc)
    session = Session(
        agent=Agent.CLAUDE,
        session_id=SESSION_ID,
        cwd=current,
        created_at=timestamp,
        updated_at=timestamp,
        transcript_path=transcript,
    )
    return provider, session, history, debug_file, telemetry_file


def test_claude_delete_removes_only_target_session_data(tmp_path):
    provider, session, history, debug_file, telemetry_file = create_deletion_fixture(
        tmp_path
    )

    preview = provider.preview_delete(session)
    assert any("history.jsonl" in item for item in preview.items)

    result = provider.delete(session, runner=object())

    assert result.success, result.remaining
    assert not session.transcript_path.exists()
    assert not session.transcript_path.with_suffix("").exists()
    assert not debug_file.exists()
    assert not telemetry_file.exists()
    history_text = history.read_text(encoding="utf-8")
    assert SESSION_ID not in history_text
    assert OTHER_ID in history_text
    assert "not-json" in history_text

    state = json.loads(provider.state_file.read_text(encoding="utf-8"))
    project_state = state["projects"][str(session.cwd)]
    assert project_state["hasTrustDialogAccepted"] is True
    assert project_state["lastVersionBase"] == "2.1"
    assert "lastSessionId" not in project_state
    assert "lastSessionMetrics" not in project_state


def test_claude_delete_refuses_live_session(tmp_path):
    provider, session, *_ = create_deletion_fixture(tmp_path)
    registry = provider.config_dir / "sessions" / "99999999.json"
    registry.write_text(
        json.dumps({"sessionId": SESSION_ID, "pid": os.getpid()}), encoding="utf-8"
    )

    with pytest.raises(ProviderError, match="仍由进程"):
        provider.preview_delete(session)


def test_windows_pid_check_does_not_call_os_kill(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(claude_module.os, "name", "nt")
    monkeypatch.setattr(
        ClaudeProvider,
        "_windows_pid_is_alive",
        staticmethod(lambda pid: calls.append(pid) or True),
    )
    monkeypatch.setattr(
        claude_module.os,
        "kill",
        lambda *_: pytest.fail("Windows must not use os.kill for a read-only PID check"),
    )

    assert ClaudeProvider._pid_is_alive(1234) is True
    assert calls == [1234]


def test_atomic_write_refuses_to_overwrite_a_concurrent_change(tmp_path):
    path = tmp_path / "shared.jsonl"
    path.write_text("before\n", encoding="utf-8")
    expected_signature = ClaudeProvider._stat_signature(path.stat())
    path.write_text("changed by another process\n", encoding="utf-8")

    with pytest.raises(OSError, match="已被其他进程修改"):
        ClaudeProvider._atomic_write_text(path, "sess update\n", expected_signature)

    assert path.read_text(encoding="utf-8") == "changed by another process\n"
