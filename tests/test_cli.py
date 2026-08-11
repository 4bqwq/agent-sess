from __future__ import annotations

import pytest

from sess.cli import run, split_native_args
from sess.metadata import MetadataStore
from sess.models import Agent
from sess.service import SessionService

from test_service import (
    CODEX_ID,
    CapturingRunner,
    StubProvider,
    make_service,
    make_session,
)


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
