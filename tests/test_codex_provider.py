from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sess.models import Agent, Session, TraceEventKind
from sess.providers.codex import CodexProvider


SESSION_ID = "019fec4f-4ba2-7700-9fbf-a55f74a2dca7"
FORKED_SESSION_ID = "019ff0ca-1692-7eb0-bded-7c744980f79f"


def write_jsonl(path: Path, events: list[object], invalid_line: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event) for event in events]
    if invalid_line:
        lines.insert(1, "{invalid")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def meta_event(
    cwd: Path,
    *,
    session_id: str = SESSION_ID,
    timestamp: str = "2026-08-11T00:00:00Z",
    originator: str = "codex-tui",
    source: object = "cli",
):
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "cwd": str(cwd),
            "originator": originator,
            "source": source,
        },
    }


def make_trace_session(transcript: Path, cwd: Path) -> Session:
    timestamp = datetime(2026, 8, 11, tzinfo=timezone.utc)
    return Session(
        agent=Agent.CODEX,
        session_id=SESSION_ID,
        cwd=cwd,
        created_at=timestamp,
        updated_at=timestamp,
        transcript_path=transcript,
    )


def test_discover_keeps_only_exact_interactive_cwd(tmp_path):
    codex_home = tmp_path / "codex-home"
    current = tmp_path / "project"
    current.mkdir()
    sessions = codex_home / "sessions" / "2026" / "08" / "11"

    write_jsonl(
        sessions / "interactive.jsonl",
        [
            meta_event(current),
            {"timestamp": "2026-08-11T02:00:00Z", "type": "event_msg"},
        ],
        invalid_line=True,
    )
    write_jsonl(
        sessions / "exec.jsonl",
        [meta_event(current, originator="codex-exec")],
    )
    child = current / "child"
    child.mkdir()
    write_jsonl(sessions / "child.jsonl", [meta_event(child)])

    result = CodexProvider(codex_home=codex_home).discover(current)

    assert [session.session_id for session in result.sessions] == [SESSION_ID]
    assert result.sessions[0].updated_at.hour == 2
    assert any("无效 JSON 行" in warning for warning in result.warnings)


def test_discover_new_only_reads_paths_created_after_snapshot(tmp_path):
    codex_home = tmp_path / "codex-home"
    current = tmp_path / "project"
    current.mkdir()
    sessions = codex_home / "sessions" / "2026" / "08" / "11"
    existing = sessions / f"rollout-existing-{SESSION_ID}.jsonl"
    write_jsonl(existing, [meta_event(current)])
    provider = CodexProvider(codex_home=codex_home)
    snapshot = provider.snapshot_transcript_paths()

    write_jsonl(
        sessions / f"rollout-new-{FORKED_SESSION_ID}.jsonl",
        [meta_event(current, session_id=FORKED_SESSION_ID)],
    )

    result = provider.discover_new(current, snapshot)

    assert [session.session_id for session in result.sessions] == [
        FORKED_SESSION_ID
    ]


def test_discover_fork_keeps_child_identity_from_first_session_meta(tmp_path):
    codex_home = tmp_path / "codex-home"
    current = tmp_path / "project"
    current.mkdir()
    sessions = codex_home / "sessions" / "2026" / "08" / "11"

    write_jsonl(
        sessions / f"rollout-parent-{SESSION_ID}.jsonl",
        [
            meta_event(
                current,
                timestamp="2026-08-03T13:27:51Z",
            ),
            {"timestamp": "2026-08-11T11:57:00Z", "type": "event_msg"},
        ],
    )
    child_meta = meta_event(
        current,
        session_id=FORKED_SESSION_ID,
        timestamp="2026-08-11T12:26:46Z",
    )
    child_meta["payload"]["forked_from_id"] = SESSION_ID
    write_jsonl(
        sessions / f"rollout-child-{FORKED_SESSION_ID}.jsonl",
        [
            child_meta,
            meta_event(
                current,
                timestamp="2026-08-11T12:26:46Z",
            ),
            {"timestamp": "2026-08-11T12:38:00Z", "type": "event_msg"},
        ],
    )

    result = CodexProvider(codex_home=codex_home).discover(current)

    assert {session.session_id for session in result.sessions} == {
        SESSION_ID,
        FORKED_SESSION_ID,
    }
    forked = next(
        session
        for session in result.sessions
        if session.session_id == FORKED_SESSION_ID
    )
    assert forked.created_at.hour == 12
    assert forked.created_at.minute == 26
    assert forked.updated_at.hour == 12
    assert forked.updated_at.minute == 38
    assert forked.forked_from_id == SESSION_ID


@pytest.fixture(params=["new", "resume", "fork"])
def codex_launch(request):
    provider = CodexProvider(codex_home=Path("/unused"), executable="codex-test")
    command = request.param
    prefix = ["codex-test"]
    if command != "new":
        prefix.extend([command, SESSION_ID])

    def launch(native_args):
        if command == "new":
            return provider.new_argv(native_args)
        return getattr(provider, f"{command}_argv")(SESSION_ID, native_args)

    return launch, prefix


def test_codex_commands_default_to_full_access_without_approval(codex_launch):
    launch, prefix = codex_launch

    assert launch([]) == [
        *prefix,
        "--sandbox",
        "danger-full-access",
        "--ask-for-approval",
        "never",
    ]


def test_codex_commands_preserve_forwarded_argument_order(codex_launch):
    launch, prefix = codex_launch
    native_args = ["-c", 'model="gpt-test"', "--model", "gpt-test", "prompt"]

    assert launch(native_args) == [
        *prefix,
        "--sandbox",
        "danger-full-access",
        "--ask-for-approval",
        "never",
        *native_args,
    ]


@pytest.mark.parametrize(
    "native_args",
    [
        ["--sandbox", "workspace-write"],
        ["--sandbox=workspace-write"],
        ["-s", "read-only"],
        ["-sread-only"],
        ["-s=read-only"],
    ],
)
def test_codex_explicit_sandbox_only_overrides_sandbox_default(
    codex_launch, native_args
):
    launch, prefix = codex_launch

    assert launch(native_args) == [
        *prefix,
        "--ask-for-approval",
        "never",
        *native_args,
    ]


@pytest.mark.parametrize(
    "native_args",
    [
        ["--ask-for-approval", "on-request"],
        ["--ask-for-approval=on-request"],
        ["-a", "on-request"],
        ["-aon-request"],
        ["-a=on-request"],
    ],
)
def test_codex_explicit_approval_only_overrides_approval_default(
    codex_launch, native_args
):
    launch, prefix = codex_launch

    assert launch(native_args) == [
        *prefix,
        "--sandbox",
        "danger-full-access",
        *native_args,
    ]


@pytest.mark.parametrize(
    "native_args",
    [
        ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
        ["-aon-request", "--model", "gpt-test", "-s=workspace-write", "prompt"],
        ["--sandbox=workspace-write", "--ask-for-approval=on-request"],
    ],
)
def test_codex_explicit_permissions_override_both_defaults(
    codex_launch, native_args
):
    launch, prefix = codex_launch

    assert launch(native_args) == [*prefix, *native_args]


@pytest.mark.parametrize(
    ("native_args", "remaining_defaults"),
    [
        (["-c", 'sandbox_mode="workspace-write"'], ["--ask-for-approval", "never"]),
        (["--config", 'sandbox_mode="read-only"'], ["--ask-for-approval", "never"]),
        (["--config=sandbox_mode=workspace-write"], ["--ask-for-approval", "never"]),
        (["-csandbox_mode=read-only"], ["--ask-for-approval", "never"]),
        (["-c=sandbox_mode=read-only"], ["--ask-for-approval", "never"]),
        (["-c", ' sandbox_mode = "read-only" '], ["--ask-for-approval", "never"]),
        (["-c", 'approval_policy="on-request"'], ["--sandbox", "danger-full-access"]),
        (["--config=approval_policy=on-request"], ["--sandbox", "danger-full-access"]),
        (["-capproval_policy=on-request"], ["--sandbox", "danger-full-access"]),
        (["-c", ' approval_policy = "on-request" '], ["--sandbox", "danger-full-access"]),
        (
            ["-c", "approval_policy.granular.sandbox_approval=true"],
            ["--sandbox", "danger-full-access"],
        ),
        (
            ["-c", "sandbox_mode=read-only", "--config", "approval_policy=on-request"],
            [],
        ),
    ],
)
def test_codex_permission_config_overrides_matching_defaults(
    codex_launch, native_args, remaining_defaults
):
    launch, prefix = codex_launch

    assert launch(native_args) == [*prefix, *remaining_defaults, *native_args]


@pytest.mark.parametrize(
    "flag",
    [
        "--dangerously-bypass-approvals-and-sandbox",
        "--yolo",
        "--approve-for-me",
        "--full-auto",
    ],
)
def test_codex_combined_permission_flags_override_both_defaults(codex_launch, flag):
    launch, prefix = codex_launch
    native_args = ["--model", "gpt-test", flag, "prompt"]

    assert launch(native_args) == [*prefix, *native_args]


@pytest.mark.parametrize(
    "native_args",
    [
        ["--", "--sandbox", "read-only", "--ask-for-approval", "on-request"],
        ["--", "--yolo"],
        ["--", "--approve-for-me"],
        ["--", "-c", "sandbox_mode=read-only"],
        ["Explain --sandbox and --ask-for-approval"],
        ["--model", "--sandbox"],
        ["-m", "--ask-for-approval"],
        ["--profile", "--full-auto"],
        ["-p", "--yolo"],
        ["--image", "--ask-for-approval"],
        ["--add-dir", "--yolo"],
        ["--enable", "--sandbox"],
        ["--disable", "--ask-for-approval"],
        ["-C", "--full-auto"],
        ["--remote", "--yolo"],
        ["--remote-auth-token-env", "--approve-for-me"],
        ["-c", 'model="--sandbox"'],
        ["--config", 'other="--ask-for-approval"'],
        ["--config=other=--yolo"],
        ["-c", 'sandbox_permissions=["disk-full-read-access"]'],
        ["-c", "profiles.work.sandbox_mode=read-only"],
    ],
)
def test_codex_permission_like_values_and_prompts_do_not_override_defaults(
    codex_launch, native_args
):
    launch, prefix = codex_launch

    assert launch(native_args) == [
        *prefix,
        "--sandbox",
        "danger-full-access",
        "--ask-for-approval",
        "never",
        *native_args,
    ]


def test_codex_only_options_before_native_terminator_override_defaults(codex_launch):
    launch, prefix = codex_launch
    native_args = ["--sandbox=read-only", "--", "--ask-for-approval=on-request"]

    assert launch(native_args) == [
        *prefix,
        "--ask-for-approval",
        "never",
        *native_args,
    ]


@pytest.mark.parametrize("flag", ["--last", "--all", "--include-non-interactive"])
def test_codex_passes_session_selection_flags_to_native_cli(flag):
    provider = CodexProvider(codex_home=Path("/unused"))

    assert provider.resume_argv(SESSION_ID, [flag])[-1] == flag


def test_codex_allows_config_short_flag():
    provider = CodexProvider(codex_home=Path("/unused"))

    assert provider.resume_argv(SESSION_ID, ["-c", "model=x"])[-2:] == [
        "-c",
        "model=x",
    ]


def test_codex_trace_uses_latest_task_and_excludes_control_content(tmp_path):
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    events = [
        meta_event(tmp_path),
        {
            "timestamp": "2026-08-11T01:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-11T01:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "old request"}],
            },
        },
        {
            "timestamp": "2026-08-11T01:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "old answer"}],
            },
        },
        {
            "timestamp": "2026-08-11T02:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-11T02:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "latest request"}],
            },
        },
        {
            "timestamp": "2026-08-11T02:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<skill>hidden</skill>"}],
            },
        },
        {
            "timestamp": "2026-08-11T02:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [
                    "visible summary",
                    {"type": "summary_text", "text": "second summary"},
                ],
                "encrypted_content": "hidden reasoning",
            },
        },
        {
            "timestamp": "2026-08-11T02:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "checking files"}],
            },
        },
        {
            "timestamp": "2026-08-11T02:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-1",
                "input": json.dumps({"cmd": "rg session"}),
            },
        },
        {
            "timestamp": "2026-08-11T02:00:06Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": [{"type": "input_text", "text": "found session"}],
            },
        },
        {
            "timestamp": "2026-08-11T02:00:07Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "latest answer"}],
            },
        },
        {
            "timestamp": "2026-08-11T02:00:08Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {
                    "message": "stream disconnected",
                    "codex_error_info": "other",
                },
            },
        },
    ]
    write_jsonl(transcript, events)
    provider = CodexProvider(codex_home=tmp_path)
    session = make_trace_session(transcript, tmp_path)

    latest = provider.read_trace(session, all_turns=False)
    latest_text = "\n".join(event.content for event in latest.events)
    assert [event.kind for event in latest.events] == [
        TraceEventKind.USER,
        TraceEventKind.REASONING,
        TraceEventKind.ASSISTANT,
        TraceEventKind.TOOL_CALL,
        TraceEventKind.TOOL_RESULT,
        TraceEventKind.ASSISTANT,
        TraceEventKind.ERROR,
    ]
    assert "latest request" in latest_text
    assert "old request" not in latest_text
    assert "<skill>" not in latest_text
    assert "hidden reasoning" not in latest_text
    assert "second summary" in latest_text
    assert "stream disconnected" in latest_text
    assert latest.events[3].label == "exec"
    assert latest.events[3].language == "json"
    assert latest.events[4].label == "exec"

    complete = provider.read_trace(session, all_turns=True)
    complete_text = "\n".join(event.content for event in complete.events)
    assert "old request" in complete_text
    assert "old answer" in complete_text
    assert "<skill>" not in complete_text

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


def test_codex_trace_ignores_an_incomplete_trailing_line(tmp_path):
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    event = {
        "timestamp": "2026-08-11T01:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "request"}],
        },
    }
    transcript.write_text(json.dumps(event) + "\n{\"partial\"", encoding="utf-8")
    provider = CodexProvider(codex_home=tmp_path)

    trace = provider.read_trace(
        make_trace_session(transcript, tmp_path), all_turns=False
    )

    assert [item.content for item in trace.events] == ["request"]
    assert any("尚未完整写入的末行" in warning for warning in trace.warnings)

    conversation = provider.read_conversation(
        make_trace_session(transcript, tmp_path)
    )
    assert conversation.turns[0].user == "request"
    assert conversation.turns[0].assistant is None


def test_codex_delete_uses_native_force_and_verifies_transcript(tmp_path):
    transcript = tmp_path / f"{SESSION_ID}.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    provider = CodexProvider(codex_home=tmp_path, executable="codex-test")

    class Runner:
        argv = None

        def run(self, argv):
            self.argv = argv
            transcript.unlink()
            return 0

    runner = Runner()
    model = Session(
        agent=Agent.CODEX,
        session_id=SESSION_ID,
        cwd=tmp_path,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        transcript_path=transcript,
    )

    result = provider.delete(model, runner)

    assert runner.argv == ["codex-test", "delete", "--force", SESSION_ID]
    assert result.success
