from __future__ import annotations

from sess.cli import run, split_native_args

from test_service import CODEX_ID, make_service


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

