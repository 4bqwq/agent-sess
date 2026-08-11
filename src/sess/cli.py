from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from sess.errors import SessError
from sess.models import Catalog
from sess.service import SessionService
from sess.tui import SessionTui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sess",
        description="管理当前目录的 Codex CLI 与 Claude Code 交互式会话",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ls", help="打开当前目录的会话列表")

    resume_parser = subparsers.add_parser("resume", help="恢复一个会话")
    resume_parser.add_argument("target", metavar="ID|name")

    rename_parser = subparsers.add_parser("rename", help="设置 sess 本地名称")
    rename_parser.add_argument("target", metavar="ID|name")
    rename_parser.add_argument("new_name", metavar="new-name")

    fork_parser = subparsers.add_parser("fork", help="分叉并进入一个新会话")
    fork_parser.add_argument("target", metavar="ID|name")

    delete_parser = subparsers.add_parser("del", help="从硬盘永久删除一个会话")
    delete_parser.add_argument("target", metavar="ID|name")
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
    command = namespace.command or "ls"

    if native_args and command not in {"resume", "fork"}:
        parser.error("只有 resume 和 fork 支持在 -- 后透传原生参数")

    selected_service = service or SessionService(Path.cwd())
    try:
        if command == "ls":
            return _run_list(selected_service)
        if command == "resume":
            return selected_service.resume(namespace.target, native_args)
        if command == "rename":
            session = selected_service.rename(namespace.target, namespace.new_name)
            print(
                f"已命名 {session.agent.value}:{session.session_id} -> {namespace.new_name}"
            )
            return 0
        if command == "fork":
            return selected_service.fork(namespace.target, native_args)
        if command == "del":
            return _run_delete(selected_service, namespace.target, namespace.yes)
        parser.error(f"未知命令: {command}")
    except SessError as exc:
        print(f"sess: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


def _run_list(service: SessionService) -> int:
    catalog = service.catalog()
    selected = SessionTui(catalog, service.project_dir).run()
    _emit_warnings(catalog)
    if selected is None:
        return 0
    return service.resume_session(selected)


def _emit_warnings(catalog: Catalog) -> None:
    for warning in catalog.warnings:
        print(f"sess warning: {warning}", file=sys.stderr)


def _run_delete(service: SessionService, target: str, assume_yes: bool) -> int:
    session = service.resolve(target)
    if assume_yes and not _is_full_uuid(target, session.session_id):
        raise SessError("--yes 只能与完整 session ID 一起使用，不能与名称搭配")
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
    if target != session_id:
        return False
    try:
        return str(uuid.UUID(target)) == target.lower()
    except ValueError:
        return False


def main() -> None:
    raise SystemExit(run())
