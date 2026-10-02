from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sess.models import Agent, Catalog, Session
from sess.tui import (
    display_width,
    fit_cell,
    format_time,
    move_selection,
    render_plain,
    render_screen,
)


def make_session(index: int) -> Session:
    timestamp = datetime(2026, 8, 11, index, tzinfo=timezone.utc)
    return Session(
        agent=Agent.CODEX if index % 2 == 0 else Agent.CLAUDE,
        session_id=f"00000000-0000-4000-8000-{index:012d}",
        cwd=Path("/project"),
        created_at=timestamp,
        updated_at=timestamp,
        transcript_path=Path(f"/{index}.jsonl"),
        name=f"会话-{index}",
    )


def test_navigation_crosses_pages_and_stops_at_bounds():
    assert move_selection(0, "k", 8, 3) == 0
    assert move_selection(0, "j", 8, 3) == 1
    assert move_selection(2, "j", 8, 3) == 3
    assert move_selection(3, "k", 8, 3) == 2
    assert move_selection(4, "h", 8, 3) == 1
    assert move_selection(1, "l", 8, 3) == 4
    assert move_selection(7, "l", 8, 3) == 7


def test_fit_cell_counts_wide_characters():
    assert fit_cell("中文a", 6) == "中文a "
    assert fit_cell("中文abc", 5) == "中文…"
    assert fit_cell("a\x1b[2J", 6) == "a?[2J "


def test_renderers_include_required_columns_and_full_selected_id():
    sessions = tuple(make_session(index) for index in range(4))
    catalog = Catalog(sessions)

    plain = render_plain(catalog)
    lines = plain.splitlines()
    assert "\t" not in plain
    header_id_column = display_width(lines[0][: lines[0].index("SESSION_ID")])
    header_created_column = display_width(lines[0][: lines[0].index("CREATED")])
    header_updated_column = display_width(lines[0][: lines[0].index("UPDATED")])
    for line, session in zip(lines[1:], sessions):
        assert display_width(line[: line.index(session.session_id)]) == header_id_column
        created = format_time(session.created_at)
        updated = format_time(session.updated_at)
        assert display_width(line[: line.index(created)]) == header_created_column
        assert display_width(line[: line.rindex(updated)]) == header_updated_column

    screen = render_screen(catalog, Path("/project"), 1, 100, 14)
    assert "AGENT" in screen
    assert "SESSION_ID" in screen
    assert sessions[1].session_id in screen
