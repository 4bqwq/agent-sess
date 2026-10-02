from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime

from sess.models import (
    SessionConversation,
    SessionTrace,
    TraceEvent,
    TraceEventKind,
)


def render_conversation_markdown(conversation: SessionConversation) -> str:
    session = conversation.session
    lines = [
        "# Session conversation",
        "",
        f"- Agent: {_inline_code(session.agent.value)}",
        f"- Session ID: {_inline_code(session.session_id)}",
    ]
    if session.name:
        lines.append(f"- Name: {_inline_code(session.name)}")
    lines.extend(
        (
            f"- Created: {_format_time(session.created_at)}",
            f"- Updated: {_format_time(session.updated_at)}",
        )
    )

    if not conversation.turns:
        lines.extend(("", "当前会话没有可显示的用户轮次。"))
    for index, turn in enumerate(conversation.turns, start=1):
        lines.extend(
            (
                "",
                f"## Turn {index}",
                "",
                "### User",
                "",
                turn.user,
                "",
                "### Assistant",
                "",
                turn.assistant or "（尚无最终输出）",
            )
        )
    return "\n".join(lines) + "\n"


def render_trace_markdown(trace: SessionTrace) -> str:
    session = trace.session
    lines = [
        "# Session transcript",
        "",
        f"- Agent: {_inline_code(session.agent.value)}",
        f"- Session ID: {_inline_code(session.session_id)}",
    ]
    if session.name:
        lines.append(f"- Name: {_inline_code(session.name)}")
    lines.extend(
        (
            f"- Scope: {'full session' if trace.all_turns else 'latest turn'}",
            f"- Created: {_format_time(session.created_at)}",
            f"- Updated: {_format_time(session.updated_at)}",
        )
    )

    for event in _merge_adjacent_events(trace.events):
        lines.extend(("", _event_heading(event), ""))
        if event.kind in {
            TraceEventKind.TOOL_CALL,
            TraceEventKind.TOOL_RESULT,
            TraceEventKind.TOOL_ERROR,
        }:
            lines.append(_fenced_block(event.content, event.language or "text"))
        else:
            lines.append(event.content)
    return "\n".join(lines) + "\n"


def _merge_adjacent_events(events: tuple[TraceEvent, ...]) -> list[TraceEvent]:
    merged: list[TraceEvent] = []
    mergeable = {TraceEventKind.ASSISTANT, TraceEventKind.REASONING}
    for event in events:
        if (
            merged
            and event.kind in mergeable
            and merged[-1].kind is event.kind
            and merged[-1].label == event.label
        ):
            previous = merged[-1]
            merged[-1] = replace(
                previous,
                content=f"{previous.content}\n\n{event.content}",
            )
        else:
            merged.append(event)
    return merged


def _event_heading(event: TraceEvent) -> str:
    if event.kind is TraceEventKind.USER:
        return "## User"
    if event.kind is TraceEventKind.ASSISTANT:
        suffix = f" · {event.label}" if event.label else ""
        return f"## Assistant{suffix}"
    if event.kind is TraceEventKind.REASONING:
        return "## Reasoning summary"
    if event.kind is TraceEventKind.TOOL_CALL:
        return _tool_heading("Tool call", event.label)
    if event.kind is TraceEventKind.TOOL_RESULT:
        return _tool_heading("Tool result", event.label)
    if event.kind is TraceEventKind.TOOL_ERROR:
        return _tool_heading("Tool error", event.label)
    return "## Error"


def _tool_heading(title: str, label: str | None) -> str:
    return f"### {title}" + (f" · {_inline_code(label)}" if label else "")


def _fenced_block(content: str, language: str) -> str:
    runs = [len(match.group(0)) for match in re.finditer(r"`+", content)]
    fence = "`" * max(3, (max(runs) + 1) if runs else 3)
    ending = "" if content.endswith("\n") else "\n"
    return f"{fence}{language}\n{content}{ending}{fence}"


def _inline_code(value: str) -> str:
    runs = [len(match.group(0)) for match in re.finditer(r"`+", value)]
    fence = "`" * ((max(runs) + 1) if runs else 1)
    padding = " " if value.startswith(("`", " ")) or value.endswith(("`", " ")) else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def _format_time(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
