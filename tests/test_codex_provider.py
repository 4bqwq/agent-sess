from __future__ import annotations

import json
from pathlib import Path

import pytest

from sess.providers.codex import CodexProvider


SESSION_ID = "019fec4f-4ba2-7700-9fbf-a55f74a2dca7"


def write_jsonl(path: Path, events: list[object], invalid_line: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event) for event in events]
    if invalid_line:
        lines.insert(1, "{invalid")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def meta_event(cwd: Path, *, originator: str = "codex-tui", source: object = "cli"):
    return {
        "timestamp": "2026-08-11T00:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": SESSION_ID,
            "cwd": str(cwd),
            "originator": originator,
            "source": source,
        },
    }


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


def test_codex_commands_preserve_forwarded_argument_order():
    provider = CodexProvider(codex_home=Path("/unused"), executable="codex-test")
    native_args = ["-c", 'model="gpt-test"', "--model", "gpt-test", "prompt"]

    assert provider.resume_argv(SESSION_ID, native_args) == [
        "codex-test",
        "resume",
        SESSION_ID,
        *native_args,
    ]
    assert provider.fork_argv(SESSION_ID, native_args) == [
        "codex-test",
        "fork",
        SESSION_ID,
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
    from datetime import datetime, timezone

    from sess.models import Agent, Session

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
