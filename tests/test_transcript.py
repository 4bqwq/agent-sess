from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sess.models import (
    Agent,
    Session,
    SessionTrace,
    TraceEvent,
    TraceEventKind,
)
from sess.transcript import render_trace_markdown


def make_trace() -> SessionTrace:
    timestamp = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
    session = Session(
        agent=Agent.CODEX,
        session_id="019fec4f-4ba2-7700-9fbf-a55f74a2dca7",
        cwd=Path("/project"),
        created_at=timestamp,
        updated_at=timestamp,
        transcript_path=Path("/transcript.jsonl"),
        name="feature-a",
    )
    return SessionTrace(
        session=session,
        all_turns=False,
        events=(
            TraceEvent(TraceEventKind.USER, "inspect the project"),
            TraceEvent(
                TraceEventKind.ASSISTANT,
                "first update",
                label="Commentary",
            ),
            TraceEvent(
                TraceEventKind.ASSISTANT,
                "second update",
                label="Commentary",
            ),
            TraceEvent(
                TraceEventKind.TOOL_CALL,
                '{\n  "cmd": "printf ```"\n}',
                label="exec",
                language="json",
            ),
            TraceEvent(
                TraceEventKind.TOOL_RESULT,
                "result\n```\ninside",
                label="exec",
                language="text",
            ),
            TraceEvent(TraceEventKind.REASONING, "public summary"),
            TraceEvent(
                TraceEventKind.ASSISTANT,
                "finished",
                label="Final",
            ),
        ),
    )


def test_trace_markdown_is_readable_and_preserves_full_content():
    rendered = render_trace_markdown(make_trace())

    assert rendered.startswith("# Session transcript\n")
    assert "- Agent: `codex`" in rendered
    assert "- Name: `feature-a`" in rendered
    assert "- Scope: latest turn" in rendered
    assert "## User\n\ninspect the project" in rendered
    assert rendered.count("## Assistant · Commentary") == 1
    assert "first update\n\nsecond update" in rendered
    assert "### Tool call · `exec`" in rendered
    assert "### Tool result · `exec`" in rendered
    assert "result\n```\ninside" in rendered
    assert "````text" in rendered
    assert "## Reasoning summary" in rendered
    assert rendered.endswith("finished\n")


def test_trace_markdown_marks_full_session_scope():
    trace = make_trace()
    complete = SessionTrace(
        session=trace.session,
        events=trace.events,
        all_turns=True,
    )

    assert "- Scope: full session" in render_trace_markdown(complete)
