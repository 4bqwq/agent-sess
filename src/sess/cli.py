from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from sess.errors import SessError
from sess.models import Catalog, LaunchResult
from sess.service import SessionService
from sess.tui import SessionTui, render_plain
from sess.transcript import render_conversation_markdown, render_trace_markdown


DEFAULT_LIST_PAGE_SIZE = 20


def positive_page(value: str) -> int:
    try:
        page = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("页码必须是正整数") from exc
    if page < 1:
        raise argparse.ArgumentTypeError("页码必须是正整数")
    return page


def positive_turn_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("显示轮数必须是正整数") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("显示轮数必须是正整数")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sess",
        description="管理当前目录的 Codex CLI 与 Claude Code 交互式会话",
    )
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("ls", help="输出当前目录的会话列表")
    list_scope = list_parser.add_mutually_exclusive_group()
    list_scope.add_argument(
        "--page",
        type=positive_page,
        default=1,
        metavar="N",
        help=f"输出第 N 页（默认每页 {DEFAULT_LIST_PAGE_SIZE} 条）",
    )
    list_scope.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help="输出全部会话",
    )

    new_parser = subparsers.add_parser("new", help="启动一个新的 Codex 会话")
    new_parser.add_argument("name", nargs="?", metavar="name")
    new_parser.add_argument(
        "--fork",
        dest="fork_target",
        metavar="ID|name|prefix",
        help="从指定会话分叉；未指定 name 时自动命名",
    )

    resume_parser = subparsers.add_parser("resume", help="恢复一个会话")
    resume_parser.add_argument("target", metavar="ID|name|prefix")

    rename_parser = subparsers.add_parser("rename", help="设置 sess 本地名称")
    rename_parser.add_argument("target", metavar="ID|name|prefix")
    rename_parser.add_argument("new_name", metavar="new-name")

    fork_parser = subparsers.add_parser("fork", help="分叉并进入一个新会话")
    fork_parser.add_argument("target", metavar="ID|name|prefix")

    print_parser = subparsers.add_parser("print", help="输出会话轨迹的 Markdown 源码")
    print_parser.add_argument("target", metavar="ID|name|prefix")
    print_parser.add_argument(
        "--all",
        dest="all_turns",
        action="store_true",
        help="输出整个 session，而不是最后一轮",
    )

    path_parser = subparsers.add_parser("path", help="输出原始 transcript 的绝对路径")
    path_parser.add_argument("target", metavar="ID|name|prefix")

    show_parser = subparsers.add_parser("show", help="显示最近的最终问答")
    show_parser.add_argument("target", metavar="ID|name|prefix")
    show_parser.add_argument(
        "-n",
        "--turns",
        type=positive_turn_count,
        default=1,
        metavar="N",
        help="显示最近 N 轮最终问答（默认 1）",
    )

    delete_parser = subparsers.add_parser("del", help="从硬盘永久删除一个会话")
    delete_parser.add_argument("target", metavar="ID|name|prefix")
    delete_parser.add_argument(
        "--yes", action="store_true", help="跳过确认；只允许搭配完整 session ID"
    )
    return parser


def split_native_args(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    arguments = list(argv)
    try:
        separator = arguments.index("--")
    except ValueError:
        return arguments, []
    return arguments[:separator], arguments[separator + 1 :]


def run(argv: Sequence[str] | None = None, service: SessionService | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    sess_arguments, native_args = split_native_args(arguments)
    parser = build_parser()
    namespace = parser.parse_args(sess_arguments)
    command = namespace.command

    if native_args and command not in {"new", "resume", "fork"}:
        parser.error("只有 new、resume 和 fork 支持在 -- 后透传原生参数")

    selected_service = service or SessionService(Path.cwd())
    try:
        if command is None:
            return _run_interactive(selected_service)
        if command == "ls":
            return _run_list(
                selected_service,
                page=namespace.page,
                show_all=namespace.show_all,
            )
        if command == "new":
            result = selected_service.new(
                name=namespace.name,
                fork_target=namespace.fork_target,
                native_args=native_args,
            )
            _emit_launch_name(
                result,
                automatic=(
                    namespace.name is None
                    and namespace.fork_target is not None
                ),
            )
            return result.return_code
        if command == "resume":
            return selected_service.resume(namespace.target, native_args)
        if command == "rename":
            session = selected_service.rename(namespace.target, namespace.new_name)
            print(
                f"已命名 {session.agent.value}:{session.session_id} -> {namespace.new_name}"
            )
            return 0
        if command == "fork":
            result = selected_service.fork(namespace.target, native_args)
            _emit_launch_name(result, automatic=True)
            return result.return_code
        if command == "print":
            return _run_print(
                selected_service,
                namespace.target,
                all_turns=namespace.all_turns,
            )
        if command == "path":
            print(selected_service.transcript_path(namespace.target))
            return 0
        if command == "show":
            return _run_show(
                selected_service,
                namespace.target,
                turn_count=namespace.turns,
            )
        if command == "del":
            return _run_delete(selected_service, namespace.target, namespace.yes)
        parser.error(f"未知命令: {command}")
    except SessError as exc:
        print(f"sess: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


def _run_interactive(service: SessionService) -> int:
    catalog = service.catalog()
    selected = SessionTui(catalog, service.project_dir).run()
    _emit_warnings(catalog)
    if selected is None:
        return 0
    return service.resume_session(selected)


def _run_list(service: SessionService, page: int, show_all: bool) -> int:
    catalog = service.catalog()
    total_sessions = len(catalog.sessions)
    if show_all:
        visible_catalog = catalog
    else:
        total_pages = max(
            1,
            (total_sessions + DEFAULT_LIST_PAGE_SIZE - 1)
            // DEFAULT_LIST_PAGE_SIZE,
        )
        if page > total_pages:
            raise SessError(
                f"页码 {page} 超出范围；当前共有 {total_pages} 页、"
                f"{total_sessions} 条会话"
            )
        start = (page - 1) * DEFAULT_LIST_PAGE_SIZE
        stop = start + DEFAULT_LIST_PAGE_SIZE
        visible_catalog = Catalog(catalog.sessions[start:stop])

    print(render_plain(visible_catalog))
    _emit_warnings(catalog)
    if not show_all and total_sessions > DEFAULT_LIST_PAGE_SIZE:
        print(
            f"sess: 第 {page}/{total_pages} 页，共 {total_sessions} 条会话",
            file=sys.stderr,
        )
    return 0


def _emit_warnings(catalog: Catalog) -> None:
    for warning in catalog.warnings:
        print(f"sess warning: {warning}", file=sys.stderr)


def _emit_launch_name(result: LaunchResult, *, automatic: bool) -> None:
    if result.session is None or result.name is None:
        return
    action = "已自动命名" if automatic else "已命名"
    print(
        f"{action} {result.session.agent.value}:{result.session.session_id}"
        f" -> {result.name}"
    )


def _run_print(
    service: SessionService, target: str, all_turns: bool
) -> int:
    trace = service.read_trace(target, all_turns)
    _write_utf8_stdout(render_trace_markdown(trace))
    for warning in trace.warnings:
        print(f"sess warning: {warning}", file=sys.stderr)
    return 0


def _run_show(
    service: SessionService, target: str, turn_count: int
) -> int:
    conversation = service.read_conversation(target, turn_count)
    _write_utf8_stdout(render_conversation_markdown(conversation))
    for warning in conversation.warnings:
        print(f"sess warning: {warning}", file=sys.stderr)
    return 0


def _write_utf8_stdout(value: str) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(value)
        return
    sys.stdout.flush()
    buffer.write(value.encode("utf-8"))
    buffer.flush()


def _run_delete(service: SessionService, target: str, assume_yes: bool) -> int:
    session = service.resolve(target)
    if assume_yes and not _is_full_uuid(target, session.session_id):
        raise SessError(
            "--yes 只能与完整 session ID 一起使用，不能与名称或 ID 前缀搭配"
        )
    preview = service.preview_delete(session)

    print(
        f"将永久删除 {session.agent.value}:{session.session_id}"
        + (f" ({session.name})" if session.name else "")
    )
    for item in preview.items:
        print(f"  - {item}")

    if not assume_yes:
        answer = input("此操作不会进入回收站，继续吗？[y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消。")
            return 0

    result = service.delete(session)
    for item in result.deleted:
        print(f"已删除: {item}")
    if result.remaining:
        print("以下对象仍然存在或未能清理：", file=sys.stderr)
        for item in result.remaining:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"已永久删除 {session.agent.value}:{session.session_id}")
    return 0


def _is_full_uuid(target: str, session_id: str) -> bool:
    if target.lower() != session_id.lower():
        return False
    try:
        return str(uuid.UUID(target)) == target.lower()
    except ValueError:
        return False


def main() -> None:
    raise SystemExit(run())
