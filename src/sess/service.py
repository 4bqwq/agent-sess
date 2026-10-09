from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sess.errors import MetadataError, ProviderError, TargetError
from sess.metadata import MetadataStore, validate_name
from sess.models import (
    Agent,
    Catalog,
    DeletionPreview,
    DeletionResult,
    LaunchResult,
    Session,
    SessionConversation,
    SessionLaunch,
    SessionTrace,
)
from sess.paths import directory_scan_cache, same_directory
from sess.process import NativeProcessRunner, ProjectLaunchLock
from sess.providers import ClaudeProvider, CodexProvider
from sess.providers.base import SessionProvider


MIN_SESSION_ID_PREFIX_LENGTH = 8


class SessionService:
    def __init__(
        self,
        project_dir: Path,
        providers: Sequence[SessionProvider] | None = None,
        metadata: MetadataStore | None = None,
        runner: NativeProcessRunner | None = None,
    ) -> None:
        self.project_dir = project_dir.resolve(strict=False)
        selected_providers = (
            tuple(providers)
            if providers is not None
            else (CodexProvider(), ClaudeProvider())
        )
        self.providers = {provider.agent: provider for provider in selected_providers}
        self.metadata = metadata or MetadataStore(self.project_dir)
        self.runner = runner or NativeProcessRunner()

    def catalog(self) -> Catalog:
        with directory_scan_cache():
            sessions: list[Session] = []
            warnings: list[str] = []
            for provider in self.providers.values():
                try:
                    result = provider.discover(self.project_dir)
                except (OSError, ProviderError) as exc:
                    warnings.append(f"{provider.agent.value}: {exc}")
                    continue
                sessions.extend(
                    session
                    for session in result.sessions
                    if same_directory(session.cwd, self.project_dir)
                )
                warnings.extend(result.warnings)

            names = self.metadata.read_names()
            named_sessions = [session.with_name(names.get(session.metadata_key)) for session in sessions]
            named_sessions.sort(
                key=lambda session: (session.updated_at, session.created_at, session.session_id),
                reverse=True,
            )
            return Catalog(tuple(named_sessions), tuple(warnings))

    def resolve(self, target: str, catalog: Catalog | None = None) -> Session:
        selected_catalog = catalog or self.catalog()
        id_matches = [
            session for session in selected_catalog.sessions if session.session_id == target
        ]
        if id_matches:
            return self._one_match(target, id_matches)

        name_matches = [
            session for session in selected_catalog.sessions if session.name == target
        ]
        if name_matches:
            return self._one_match(target, name_matches)

        normalized_target = target.lower()
        prefix_matches = [
            session
            for session in selected_catalog.sessions
            if session.session_id.lower().startswith(normalized_target)
        ]
        if prefix_matches:
            if len(target) < MIN_SESSION_ID_PREFIX_LENGTH:
                raise TargetError(
                    "session ID 前缀至少需要 "
                    f"{MIN_SESSION_ID_PREFIX_LENGTH} 个字符: {target!r}"
                )
            return self._one_match(target, prefix_matches)
        raise TargetError(
            "当前目录中找不到完整 session ID、完全匹配的名称或唯一 ID 前缀: "
            f"{target!r}"
        )

    @staticmethod
    def _one_match(target: str, matches: Sequence[Session]) -> Session:
        if len(matches) == 1:
            return matches[0]
        rendered = ", ".join(
            f"{session.agent.value}:{session.session_id}" for session in matches
        )
        raise TargetError(f"目标 {target!r} 对应多个会话: {rendered}")

    def rename(self, target: str, new_name: str) -> Session:
        validate_name(new_name)
        session = self.resolve(target)
        self.metadata.set_name(session.metadata_key, new_name)
        return session.with_name(new_name)

    def resume(self, target: str, native_args: Sequence[str] = ()) -> int:
        session = self.resolve(target)
        provider = self._provider_for(session.agent)
        return self.runner.run(provider.resume_argv(session.session_id, native_args))

    def resume_session(self, session: Session, native_args: Sequence[str] = ()) -> int:
        provider = self._provider_for(session.agent)
        return self.runner.run(provider.resume_argv(session.session_id, native_args))

    def new(
        self,
        name: str | None = None,
        fork_target: str | None = None,
        native_args: Sequence[str] = (),
    ) -> LaunchResult:
        if name is not None:
            validate_name(name)
            self.metadata.ensure_name_available(name)

        launch_lock = ProjectLaunchLock(self.project_dir)
        launch_lock.acquire()
        try:
            if name is not None:
                self.metadata.ensure_name_available(name)

            if fork_target is None:
                provider = self._provider_for(Agent.CODEX)
                before = Catalog(())
                parent = None
                launch = SessionLaunch(tuple(provider.new_argv(native_args)))
            else:
                before = self.catalog()
                parent = self.resolve(fork_target, before)
                provider = self._provider_for(parent.agent)
                launch = provider.prepare_fork(parent.session_id, native_args)
            known_transcript_paths = provider.snapshot_transcript_paths()

            return self._run_new_session(
                provider=provider,
                launch=launch,
                before=before,
                known_transcript_paths=known_transcript_paths,
                parent=parent,
                requested_name=name,
                launch_lock=launch_lock,
            )
        finally:
            launch_lock.release()

    def fork(
        self, target: str, native_args: Sequence[str] = ()
    ) -> LaunchResult:
        return self.new(fork_target=target, native_args=native_args)

    def _run_new_session(
        self,
        *,
        provider: SessionProvider,
        launch: SessionLaunch,
        before: Catalog,
        known_transcript_paths: frozenset[Path],
        parent: Session | None,
        requested_name: str | None,
        launch_lock: ProjectLaunchLock,
    ) -> LaunchResult:
        if launch.expected_session_id and any(
            candidate.agent is provider.agent
            and candidate.session_id == launch.expected_session_id
            for candidate in before.sessions
        ):
            raise ProviderError(
                "原生命令准备使用的新 session ID 已经存在: "
                f"{launch.expected_session_id}"
            )

        bound_session: Session | None = None
        assigned_name: str | None = None

        def observe(*, final: bool = False) -> bool:
            nonlocal bound_session, assigned_name
            discovery = provider.discover_new(
                self.project_dir, known_transcript_paths
            )
            new_sessions = tuple(
                session
                for session in discovery.sessions
                if same_directory(session.cwd, self.project_dir)
            )
            after = Catalog(
                (*before.sessions, *new_sessions),
                (*before.warnings, *discovery.warnings),
            )
            child = self._resolve_launched_session(
                provider=provider,
                parent=parent,
                before=before,
                after=after,
                expected_session_id=launch.expected_session_id,
                final=final,
            )
            if child is None:
                if (
                    final
                    and provider.agent is Agent.CODEX
                    and discovery.warnings
                ):
                    naming_failure = (
                        "，未完成命名"
                        if requested_name is not None or parent is not None
                        else ""
                    )
                    details = "\n".join(discovery.warnings)
                    raise ProviderError(
                        f"发现新增会话记录，但无法识别本次会话{naming_failure}；"
                        f"原生记录保持不变。\n原因：{details}"
                    )
                return False

            if requested_name is not None:
                self.metadata.set_name(child.metadata_key, requested_name)
                assigned_name = requested_name
            elif parent is not None:
                actual_parent = self._fork_parent(child, parent, before)
                parent_prefix = self._shortest_unique_prefix(actual_parent, after)
                assigned_name = self.metadata.assign_fork_name(
                    actual_parent.metadata_key,
                    child.metadata_key,
                    parent_prefix,
                )

            bound_session = child.with_name(assigned_name)
            launch_lock.release()
            return True

        return_code = self.runner.run_observed(launch.argv, observe)
        if bound_session is None:
            observe(final=True)
        return LaunchResult(return_code, bound_session, assigned_name)

    def read_trace(self, target: str, all_turns: bool = False) -> SessionTrace:
        session = self.resolve(target)
        provider = self._provider_for(session.agent)
        return provider.read_trace(session, all_turns)

    def transcript_path(self, target: str) -> Path:
        return self.resolve(target).transcript_path.expanduser().resolve(
            strict=False
        )

    def read_conversation(
        self, target: str, turn_count: int = 1
    ) -> SessionConversation:
        if turn_count < 1:
            raise TargetError("显示轮数必须是正整数")
        session = self.resolve(target)
        provider = self._provider_for(session.agent)
        conversation = provider.read_conversation(session)
        return SessionConversation(
            session=conversation.session,
            turns=conversation.turns[-turn_count:],
            warnings=conversation.warnings,
        )

    def preview_delete(self, session: Session) -> DeletionPreview:
        return self._provider_for(session.agent).preview_delete(session)

    def delete(self, session: Session) -> DeletionResult:
        provider = self._provider_for(session.agent)
        result = provider.delete(session, self.runner)
        if not result.success:
            return result
        try:
            removed_name = self.metadata.remove(session.metadata_key)
        except MetadataError as exc:
            return DeletionResult(
                deleted=result.deleted,
                remaining=(f"sess 名称元数据 {self.metadata.path}: {exc}",),
            )
        if removed_name:
            return DeletionResult(
                deleted=(*result.deleted, f"sess 名称 {session.name!r}"),
                remaining=(),
            )
        return result

    def _provider_for(self, agent: Agent) -> SessionProvider:
        try:
            return self.providers[agent]
        except KeyError as exc:
            raise ProviderError(f"没有为 {agent.value} 配置 Provider") from exc

    @staticmethod
    def _resolve_launched_session(
        provider: SessionProvider,
        parent: Session | None,
        before: Catalog,
        after: Catalog,
        expected_session_id: str | None,
        *,
        final: bool,
    ) -> Session | None:
        existing_keys = {session.metadata_key for session in before.sessions}
        new_sessions = [
            session
            for session in after.sessions
            if session.agent is provider.agent
            and session.metadata_key not in existing_keys
        ]
        if expected_session_id is not None:
            matches = [
                session
                for session in new_sessions
                if session.session_id == expected_session_id
            ]
            expectation = f"session ID {expected_session_id}"
        elif parent is not None:
            matches = [
                session
                for session in new_sessions
                if session.forked_from_id == parent.session_id
            ]
            expectation = f"父 session {parent.session_id}"
        else:
            matches = new_sessions
            expectation = "本次新启动"
        if len(matches) == 1:
            return matches[0]
        if not final and len(matches) == 0:
            return None
        if final and not new_sessions:
            return None

        candidates = ", ".join(
            session.session_id for session in new_sessions
        ) or "无"
        raise ProviderError(
            f"无法按 {expectation} 唯一识别新会话；"
            f"新候选: {candidates}。会话保持未命名"
        )

    @staticmethod
    def _fork_parent(
        child: Session, requested_parent: Session, before: Catalog
    ) -> Session:
        if child.forked_from_id is None:
            return requested_parent
        matches = [
            session
            for session in before.sessions
            if session.agent is child.agent
            and session.session_id == child.forked_from_id
        ]
        if len(matches) == 1:
            return matches[0]
        raise ProviderError(
            "新 fork 指向的直接父会话不在 fork 前的当前目录列表中: "
            f"{child.forked_from_id}。会话保持未命名"
        )

    @staticmethod
    def _shortest_unique_prefix(parent: Session, catalog: Catalog) -> str:
        normalized_id = parent.session_id.lower()
        for length in range(MIN_SESSION_ID_PREFIX_LENGTH, len(normalized_id) + 1):
            prefix = normalized_id[:length]
            if all(
                candidate.metadata_key == parent.metadata_key
                or not candidate.session_id.lower().startswith(prefix)
                for candidate in catalog.sessions
            ):
                return prefix
        raise ProviderError(
            "无法为父会话生成唯一 ID 前缀: "
            f"{parent.agent.value}:{parent.session_id}"
        )
