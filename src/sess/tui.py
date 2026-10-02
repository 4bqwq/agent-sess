from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Callable, Iterator

from sess.models import Catalog, Session


def page_size_for_height(height: int) -> int:
    return max(1, height - 7)


def move_selection(index: int, key: str, total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    index = min(max(index, 0), total - 1)
    if key == "j":
        return min(index + 1, total - 1)
    if key == "k":
        return max(index - 1, 0)

    row = index % page_size
    page = index // page_size
    if key == "h" and page > 0:
        return min((page - 1) * page_size + row, total - 1)
    if key == "l" and (page + 1) * page_size < total:
        return min((page + 1) * page_size + row, total - 1)
    return index


def display_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in value
    )


def safe_display_text(value: str) -> str:
    return "".join(
        "?" if unicodedata.category(character).startswith("C") else character
        for character in value
    )


def fit_cell(value: str, width: int) -> str:
    value = safe_display_text(value)
    if width <= 0:
        return ""
    if display_width(value) <= width:
        return value + " " * (width - display_width(value))
    if width == 1:
        return "…"

    output: list[str] = []
    used = 0
    for character in value:
        character_width = (
            2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        )
        if used + character_width > width - 1:
            break
        output.append(character)
        used += character_width
    return "".join(output) + "…" + " " * (width - used - 1)


def format_time(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def render_plain(catalog: Catalog) -> str:
    rows = [
        ("AGENT", "NAME", "SESSION_ID", "CREATED", "UPDATED"),
        *(
            (
                session.agent.value,
                session.name or "",
                session.session_id,
                format_time(session.created_at),
                format_time(session.updated_at),
            )
            for session in catalog.sessions
        ),
    ]
    widths = [
        max(display_width(safe_display_text(row[column])) for row in rows)
        for column in range(len(rows[0]))
    ]
    return "\n".join(
        "  ".join(
            fit_cell(value, widths[column])
            if column < len(row) - 1
            else safe_display_text(value)
            for column, value in enumerate(row)
        )
        for row in rows
    )


def render_screen(
    catalog: Catalog,
    current_dir: Path,
    index: int,
    width: int,
    height: int,
) -> str:
    sessions = catalog.sessions
    page_size = page_size_for_height(height)
    total_pages = max(1, (len(sessions) + page_size - 1) // page_size)
    page = min(index // page_size, total_pages - 1) if sessions else 0
    start = page * page_size
    stop = min(start + page_size, len(sessions))

    usable_width = max(width, 20)
    name_and_id = max(20, usable_width - 44)
    id_width = min(36, max(12, name_and_id - 12))
    name_width = min(24, max(8, name_and_id - id_width))

    lines = [fit_cell(f"sess — {current_dir}", usable_width).rstrip(), ""]
    lines.append(
        "  "
        + fit_cell("AGENT", 6)
        + " "
        + fit_cell("NAME", name_width)
        + " "
        + fit_cell("SESSION_ID", id_width)
        + " "
        + fit_cell("CREATED", 16)
        + " "
        + fit_cell("UPDATED", 16)
    )

    if sessions:
        for row_index in range(start, stop):
            session = sessions[row_index]
            marker = "> " if row_index == index else "  "
            lines.append(
                marker
                + fit_cell(session.agent.value, 6)
                + " "
                + fit_cell(session.name or "", name_width)
                + " "
                + fit_cell(session.session_id, id_width)
                + " "
                + fit_cell(format_time(session.created_at), 16)
                + " "
                + fit_cell(format_time(session.updated_at), 16)
            )
    else:
        lines.append("  当前目录没有可管理的交互式会话。")

    while len(lines) < height - 3:
        lines.append("")

    warning_note = f" | 警告 {len(catalog.warnings)}" if catalog.warnings else ""
    lines.append(
        fit_cell(
            f"第 {page + 1}/{total_pages} 页 | j/k 选择 | h/l 翻页 | Enter 恢复 | q 退出{warning_note}",
            usable_width,
        ).rstrip()
    )
    if sessions:
        selected = sessions[index]
        selected_name = f" | {selected.name}" if selected.name else ""
        lines.append(
            fit_cell(
                f"{selected.agent.value}{selected_name} | {selected.session_id}",
                usable_width,
            ).rstrip()
        )
    elif catalog.warnings:
        lines.append(fit_cell(catalog.warnings[0], usable_width).rstrip())
    else:
        lines.append("")
    return "\n".join(line[:usable_width] for line in lines[:height])


@dataclass
class SessionTui:
    catalog: Catalog
    current_dir: Path
    stdin: IO[str] = field(default_factory=lambda: sys.stdin)
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)

    def run(self) -> Session | None:
        if not self.stdin.isatty() or not self.stdout.isatty():
            self.stdout.write(render_plain(self.catalog) + "\n")
            return None
        if not self.catalog.sessions:
            self.stdout.write(render_plain(self.catalog) + "\n")
            return None

        index = 0
        with _terminal_session(self.stdin, self.stdout) as read_key:
            while True:
                terminal_size = shutil.get_terminal_size((100, 24))
                page_size = page_size_for_height(terminal_size.lines)
                screen = render_screen(
                    self.catalog,
                    self.current_dir,
                    index,
                    terminal_size.columns,
                    terminal_size.lines,
                )
                self.stdout.write("\x1b[H\x1b[2J" + screen)
                self.stdout.flush()
                key = read_key()
                if key in {"q", "\x03"}:
                    return None
                if key in {"\r", "\n"}:
                    return self.catalog.sessions[index]
                if key in {"j", "k", "h", "l"}:
                    index = move_selection(
                        index, key, len(self.catalog.sessions), page_size
                    )


@contextmanager
def _terminal_session(
    stdin: IO[str], stdout: IO[str]
) -> Iterator[Callable[[], str]]:
    _enable_windows_virtual_terminal(stdout)
    stdout.write("\x1b[?1049h\x1b[?25l")
    stdout.flush()
    try:
        if os.name == "nt":
            import msvcrt

            def read_key() -> str:
                character = msvcrt.getwch()
                if character in {"\x00", "\xe0"}:
                    msvcrt.getwch()
                    return ""
                return character

            yield read_key
        else:
            import termios
            import tty

            file_descriptor = stdin.fileno()
            previous = termios.tcgetattr(file_descriptor)
            try:
                tty.setcbreak(file_descriptor)

                def read_key() -> str:
                    return stdin.read(1)

                yield read_key
            finally:
                termios.tcsetattr(file_descriptor, termios.TCSADRAIN, previous)
    finally:
        stdout.write("\x1b[?25h\x1b[?1049l")
        stdout.flush()


def _enable_windows_virtual_terminal(stdout: IO[str]) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [ctypes.c_long]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetConsoleMode.restype = ctypes.c_int
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.SetConsoleMode.restype = ctypes.c_int
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError):
        return
