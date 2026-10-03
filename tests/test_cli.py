from __future__ import annotations

import json

import pytest

from sess.cli import run, split_native_args
from sess.metadata import MetadataStore
from sess.models import (
    Agent,
    ConversationTurn,
    LaunchResult,
    SessionConversation,
    SessionTrace,
    TraceEvent,
    TraceEventKind,
)
from sess.providers.codex import CodexProvider
from sess.service import SessionService

from test_service import (
    CLAUDE_FORK_ID,
    CLAUDE_ID,
    CODEX_ID,
    CapturingRunner,
    StubProvider,
    make_service,
    make_session,
)


CODEX_PERMISSION_DEFAULTS = [
    "--sandbox",
    "danger-full-access",
    "--ask-for-approval",
    "never",
]


def make_codex_service(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    provider = CodexProvider(codex_home=tmp_path / "codex-home")
    runner = CapturingRunner()
    service = SessionService(
        project,
        (provider,),
        MetadataStore(project),
        runner,
    )
    return service, runner


def write_codex_session(
    service,
    session_id,
    *,
    forked_from_id=None,
    source="cli",
    cwd=None,
    originator="codex-tui",
):
    provider = service.providers[Agent.CODEX]
    transcript = provider.codex_home / "sessions" / f"rollout-{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": session_id,
        "cwd": str(service.project_dir if cwd is None else cwd),
        "originator": originator,
    }
    if source is not None:
        payload["source"] = source
    if forked_from_id is not None:
        payload["forked_from_id"] = forked_from_id
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-11T00:00:00Z",
                "type": "session_meta",
                "payload": payload,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return transcript


def make_many_service(tmp_path, count=25):
    sessions = tuple(
        make_session(
            Agent.CODEX,
            f"00000000-0000-4000-8000-{index:012d}",
            tmp_path,
            index,
        )
        for index in range(count)
    )
    service = SessionService(
        tmp_path,
        (StubProvider(Agent.CODEX, sessions),),
        MetadataStore(tmp_path),
        CapturingRunner(),
    )
    return service, sessions


def test_split_native_args_uses_first_separator():
    sess_args, native_args = split_native_args(
        ["resume", "feature-a", "--", "--model", "x", "--", "prompt"]
    )

    assert sess_args == ["resume", "feature-a"]
    assert native_args == ["--model", "x", "--", "prompt"]


def test_cli_resume_forwards_arguments(tmp_path):
    service, runner = make_service(tmp_path)

    return_code = run(
        ["resume", CODEX_ID, "--", "-c", "model=x", "prompt"],
        service=service,
    )

    assert return_code == 0
    assert runner.calls[-1] == [
        "codex",
        "resume",
        CODEX_ID,
        "-c",
        "model=x",
        "prompt",
    ]


@pytest.mark.parametrize(
    ("native_args", "defaults"),
    [
        ([], CODEX_PERMISSION_DEFAULTS),
        (
            ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
            [],
        ),
        (["--sandbox", "read-only"], ["--ask-for-approval", "never"]),
        (["-a", "on-request"], ["--sandbox", "danger-full-access"]),
        (["-c", 'sandbox_mode="workspace-write"'], ["--ask-for-approval", "never"]),
        (["--full-auto"], []),
    ],
)
@pytest.mark.parametrize("source", ["cli", "vscode"])
def test_cli_codex_resume_applies_only_missing_permission_defaults(
    tmp_path, native_args, defaults, source
):
    service, runner = make_codex_service(tmp_path)
    write_codex_session(service, CODEX_ID, source=source)
    forwarded = [*native_args, "--model", "test-model", "prompt"]

    assert run(["resume", CODEX_ID[:8], "--", *forwarded], service=service) == 0
    assert runner.calls == [
        ["codex", "resume", CODEX_ID, *defaults, *forwarded]
    ]


@pytest.mark.parametrize("source", ["cli", "vscode"])
def test_cli_tui_selected_codex_session_uses_permission_defaults(
    tmp_path, monkeypatch, source
):
    service, runner = make_codex_service(tmp_path)
    write_codex_session(service, CODEX_ID, source=source)
    monkeypatch.setattr(
        "sess.cli.SessionTui.run", lambda tui: tui.catalog.sessions[0]
    )

    assert run([], service=service) == 0
    assert runner.calls == [
        ["codex", "resume", CODEX_ID, *CODEX_PERMISSION_DEFAULTS]
    ]


@pytest.mark.parametrize(
    ("arguments", "forked", "expected_name"),
    [
        (["new", "experiment"], False, "experiment"),
        (["fork", CODEX_ID[:8]], True, "019fec4f-fork1"),
        (["new", "experiment", "--fork", CODEX_ID[:8]], True, "experiment"),
    ],
)
@pytest.mark.parametrize(
    ("native_args", "defaults"),
    [
        ([], CODEX_PERMISSION_DEFAULTS),
        (["-s", "workspace-write", "-a", "on-request"], []),
    ],
)
@pytest.mark.parametrize("source", ["cli", "vscode"])
def test_cli_codex_new_and_fork_preserve_naming_with_permission_defaults(
    tmp_path, capsys, arguments, forked, expected_name, native_args, defaults, source
):
    service, runner = make_codex_service(tmp_path)
    write_codex_session(service, CODEX_ID, source=source)
    child_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    runner.return_code = 130

    def create_session(_argv):
        write_codex_session(
            service,
            child_id,
            forked_from_id=CODEX_ID if forked else None,
            source=source,
        )

    runner.on_run = create_session
    observed_names = []
    runner.after_observe = lambda observed: observed_names.append(
        (observed, service.metadata.read_names())
    )
    forwarded = [*native_args, "--model", "test-model", "prompt"]

    assert run([*arguments, "--", *forwarded], service=service) == 130
    command = ["codex", "fork", CODEX_ID] if forked else ["codex"]
    assert runner.calls == [[*command, *defaults, *forwarded]]
    assert service.metadata.read_names() == {f"codex:{child_id}": expected_name}
    assert observed_names == [(True, {f"codex:{child_id}": expected_name})]
    assert f"codex:{child_id} -> {expected_name}" in capsys.readouterr().out


def test_cli_ls_and_path_include_vscode_sessions(tmp_path, capsys):
    service, runner = make_codex_service(tmp_path)
    transcript = write_codex_session(service, CODEX_ID, source="vscode")

    assert run(["ls"], service=service) == 0
    listed = capsys.readouterr()
    assert CODEX_ID in listed.out
    assert not listed.err

    assert run(["path", CODEX_ID[:8]], service=service) == 0
    located = capsys.readouterr()
    assert located.out == f"{transcript.resolve()}\n"
    assert not located.err
    assert not runner.calls


@pytest.mark.parametrize(
    "arguments",
    [
        ["new"],
        ["new", "experiment"],
        ["fork", CODEX_ID],
        ["new", "experiment", "--fork", CODEX_ID],
    ],
)
@pytest.mark.parametrize("source", ["future-source", None, {}, [], 123])
def test_cli_new_reports_an_unrecognized_current_project_source(
    tmp_path, capsys, arguments, source
):
    service, runner = make_codex_service(tmp_path)
    write_codex_session(service, CODEX_ID)
    child_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    created = []

    def create_session(_argv):
        forked = "--fork" in arguments or arguments[0] == "fork"
        created.append(
            write_codex_session(
                service,
                child_id,
                source=source,
                forked_from_id=CODEX_ID if forked else None,
            )
        )

    runner.on_run = create_session
    runner.return_code = 130

    assert run(arguments, service=service) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "无法识别本次会话" in captured.err
    if arguments != ["new"]:
        assert "未完成命名" in captured.err
    assert "source" in captured.err
    assert str(created[0]) in captured.err
    assert service.metadata.read_names() == {}


def test_cli_named_new_reports_malformed_session_identity(tmp_path, capsys):
    service, runner = make_codex_service(tmp_path)
    runner.on_run = lambda _argv: write_codex_session(service, "invalid-session-id")

    assert run(["new", "experiment"], service=service) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "本次会话" in captured.err
    assert "session ID" in captured.err
    assert "invalid-session-id" in captured.err
    assert service.metadata.read_names() == {}


@pytest.mark.parametrize(
    "record", ["other-cwd-invalid-id", "invalid-json", "other-cwd-invalid-json"]
)
def test_cli_named_new_does_not_report_unidentifiable_unrelated_records(
    tmp_path, capsys, record
):
    service, runner = make_codex_service(tmp_path)
    runner.return_code = 130

    def create_record(_argv):
        session_id = (
            "invalid-session-id"
            if record == "other-cwd-invalid-id"
            else "791f0baf-9fb7-4d44-908a-c50b1a223a33"
        )
        transcript = write_codex_session(
            service, session_id, cwd=service.project_dir / "other"
        )
        if record == "invalid-json":
            transcript.write_text("{invalid\n", encoding="utf-8")
        elif record == "other-cwd-invalid-json":
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write("{invalid\n")

    runner.on_run = create_record

    assert run(["new", "experiment"], service=service) == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert not captured.err
    assert service.metadata.read_names() == {}


@pytest.mark.parametrize(
    "arguments",
    [
        ["new", "experiment"],
        ["fork", CODEX_ID],
        ["new", "experiment", "--fork", CODEX_ID],
    ],
)
@pytest.mark.parametrize(
    "record",
    ["none", "other-cwd", "exec", "subagent-dict", "subagent-string"],
)
def test_cli_launch_without_a_matching_interactive_record_does_not_report_failure(
    tmp_path, capsys, arguments, record
):
    service, runner = make_codex_service(tmp_path)
    write_codex_session(service, CODEX_ID)
    child_id = "791f0baf-9fb7-4d44-908a-c50b1a223a33"
    runner.return_code = 130

    def create_record(_argv):
        if record == "none":
            return
        options = {
            "other-cwd": {
                "cwd": service.project_dir / "other",
                "source": "future-source",
            },
            "exec": {"source": "exec"},
            "subagent-dict": {"source": {"subagent": {"thread_spawn": {}}}},
            "subagent-string": {"source": "subagent"},
        }
        write_codex_session(service, child_id, **options[record])

    runner.on_run = create_record

    assert run(arguments, service=service) == 130
    captured = capsys.readouterr()
    assert not captured.out
    assert not captured.err
    assert service.metadata.read_names() == {}


def test_cli_fork_reports_the_automatic_name(tmp_path, capsys, monkeypatch):
    service, _ = make_service(tmp_path)
    child = make_session(Agent.CLAUDE, CLAUDE_FORK_ID, tmp_path, 3).with_name(
        "491f0baf-fork1"
    )
    calls = []

    def fake_fork(target, native_args):
        calls.append((target, list(native_args)))
        return LaunchResult(0, child, child.name)

    monkeypatch.setattr(service, "fork", fake_fork)

    return_code = run(
        ["fork", CLAUDE_ID, "--", "--model", "test-model"],
        service=service,
    )

    captured = capsys.readouterr()
    assert return_code == 0
    assert calls == [(CLAUDE_ID, ["--model", "test-model"])]
    assert (
        f"已自动命名 claude:{CLAUDE_FORK_ID} -> 491f0baf-fork1"
        in captured.out
    )


def test_cli_new_accepts_an_optional_name_and_fork_target(
    tmp_path, capsys, monkeypatch
):
    service, _ = make_service(tmp_path)
    child = make_session(Agent.CODEX, CODEX_ID, tmp_path, 3).with_name(
        "experiment"
    )
    calls = []

    def fake_new(name, fork_target, native_args):
        calls.append((name, fork_target, list(native_args)))
        return LaunchResult(130, child, child.name)

    monkeypatch.setattr(service, "new", fake_new)

    return_code = run(
        [
            "new",
            "experiment",
            "--fork",
            CODEX_ID[:8],
            "--",
            "--model",
            "test-model",
        ],
        service=service,
    )

    captured = capsys.readouterr()
    assert return_code == 130
    assert calls == [
        ("experiment", CODEX_ID[:8], ["--model", "test-model"])
    ]
    assert f"已命名 codex:{CODEX_ID} -> experiment" in captured.out


def test_cli_new_without_arguments_starts_an_unnamed_session(
    tmp_path, capsys, monkeypatch
):
    service, _ = make_service(tmp_path)
    calls = []

    def fake_new(name, fork_target, native_args):
        calls.append((name, fork_target, list(native_args)))
        return LaunchResult(0)

    monkeypatch.setattr(service, "new", fake_new)

    assert run(["new"], service=service) == 0
    assert calls == [(None, None, [])]
    assert not capsys.readouterr().out


def test_cli_new_unnamed_fork_reports_an_automatic_name(
    tmp_path, capsys, monkeypatch
):
    service, _ = make_service(tmp_path)
    child = make_session(Agent.CODEX, CODEX_ID, tmp_path, 3).with_name(
        "019fec4f-fork1"
    )

    monkeypatch.setattr(
        service,
        "new",
        lambda name, fork_target, native_args: LaunchResult(
            0, child, child.name
        ),
    )

    assert run(["new", "--fork", CODEX_ID], service=service) == 0
    assert "已自动命名" in capsys.readouterr().out


def test_cli_yes_rejects_name_target(tmp_path, capsys):
    service, runner = make_service(tmp_path)
    service.rename(CODEX_ID, "feature-a")

    return_code = run(["del", "feature-a", "--yes"], service=service)

    assert return_code == 1
    assert not runner.calls
    assert "完整 session ID" in capsys.readouterr().err


def test_cli_yes_rejects_id_prefix(tmp_path, capsys):
    service, runner = make_service(tmp_path)

    return_code = run(["del", CODEX_ID[:8], "--yes"], service=service)

    assert return_code == 1
    assert not runner.calls
    assert "完整 session ID" in capsys.readouterr().err


def test_cli_ls_outputs_first_twenty_full_ids_without_tui(tmp_path, capsys):
    service, sessions = make_many_service(tmp_path)

    return_code = run(["ls"], service=service)

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert return_code == 0
    assert len(lines) == 21
    assert sessions[-1].session_id in captured.out
    assert sessions[4].session_id not in captured.out
    assert "第 1/2 页" in captured.err


def test_cli_ls_can_select_page_or_show_all(tmp_path, capsys):
    service, sessions = make_many_service(tmp_path)

    assert run(["ls", "--page", "2"], service=service) == 0
    second_page = capsys.readouterr()
    assert len(second_page.out.strip().splitlines()) == 6
    assert sessions[4].session_id in second_page.out
    assert sessions[5].session_id not in second_page.out
    assert "第 2/2 页" in second_page.err

    assert run(["ls", "--all"], service=service) == 0
    all_pages = capsys.readouterr()
    assert len(all_pages.out.strip().splitlines()) == 26
    assert sessions[0].session_id in all_pages.out
    assert sessions[-1].session_id in all_pages.out


def test_cli_ls_rejects_out_of_range_page(tmp_path, capsys):
    service, _ = make_many_service(tmp_path)

    return_code = run(["ls", "--page", "3"], service=service)

    captured = capsys.readouterr()
    assert return_code == 1
    assert not captured.out
    assert "当前共有 2 页" in captured.err


def test_cli_ls_rejects_non_positive_page(tmp_path):
    service, _ = make_many_service(tmp_path)

    with pytest.raises(SystemExit) as error:
        run(["ls", "--page", "0"], service=service)

    assert error.value.code == 2


def test_cli_without_subcommand_keeps_non_tty_fallback(tmp_path, capsys):
    service, sessions = make_many_service(tmp_path)

    return_code = run([], service=service)

    captured = capsys.readouterr()
    assert return_code == 0
    assert len(captured.out.strip().splitlines()) == 26
    assert sessions[0].session_id in captured.out


def test_cli_print_writes_only_markdown_to_stdout(tmp_path, capsys, monkeypatch):
    service, _ = make_service(tmp_path)
    session = service.resolve(CODEX_ID)
    calls: list[tuple[str, bool]] = []

    def fake_read_trace(target, all_turns):
        calls.append((target, all_turns))
        return SessionTrace(
            session=session,
            all_turns=all_turns,
            events=(TraceEvent(TraceEventKind.USER, "最新请求 🐈"),),
            warnings=("trailing line ignored",),
        )

    monkeypatch.setattr(service, "read_trace", fake_read_trace)

    return_code = run(
        ["print", CODEX_ID[:8], "--all"],
        service=service,
    )

    captured = capsys.readouterr()
    assert return_code == 0
    assert calls == [(CODEX_ID[:8], True)]
    assert captured.out.startswith("# Session transcript\n")
    assert "最新请求 🐈" in captured.out
    assert "warning" not in captured.out
    assert "sess warning: trailing line ignored" in captured.err


def test_cli_path_outputs_only_the_absolute_transcript_path(tmp_path, capsys):
    service, _ = make_service(tmp_path)

    return_code = run(["path", CODEX_ID[:8]], service=service)

    captured = capsys.readouterr()
    assert return_code == 0
    assert captured.out == f"{(tmp_path / f'{CODEX_ID}.jsonl').resolve()}\n"
    assert not captured.err


def test_cli_show_outputs_only_final_question_and_answer(
    tmp_path, capsys, monkeypatch
):
    service, _ = make_service(tmp_path)
    session = service.resolve(CODEX_ID)
    calls: list[tuple[str, int]] = []

    def fake_read_conversation(target, turn_count):
        calls.append((target, turn_count))
        return SessionConversation(
            session=session,
            turns=(ConversationTurn("最终问题", "最终回答"),),
            warnings=("trailing line ignored",),
        )

    monkeypatch.setattr(service, "read_conversation", fake_read_conversation)

    return_code = run(
        ["show", CODEX_ID[:8], "-n", "2"],
        service=service,
    )

    captured = capsys.readouterr()
    assert return_code == 0
    assert calls == [(CODEX_ID[:8], 2)]
    assert "# Session conversation" in captured.out
    assert "### User\n\n最终问题" in captured.out
    assert "### Assistant\n\n最终回答" in captured.out
    assert "sess warning: trailing line ignored" in captured.err


def test_cli_show_rejects_non_positive_turn_count(tmp_path):
    service, _ = make_service(tmp_path)

    with pytest.raises(SystemExit) as error:
        run(["show", CODEX_ID, "-n", "0"], service=service)

    assert error.value.code == 2
